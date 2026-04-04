"""
strategy/orb_options.py — ORB strategy that trades options instead of shares.

Signal source : QQQ ohlcv-1s  (same opening range breakout logic as orb.py)
Execution     : Buy a call (bullish breakout) or put (bearish breakout)
Pricing       : Black-Scholes estimate now; swap in OPRA data later
Sizing        : Risk-based — never risk more than max_risk_per_trade dollars

Position sizing logic:
    contracts = floor(max_risk / (ask_price × 100))

    Your maximum loss on any trade is the premium paid — no stop-loss slippage.

Exit rules:
    1. Option value drops to stop_loss_pct of entry  (e.g. 50% loss → exit)
    2. Option value rises to target_mult × entry     (e.g. 2× gain → exit)
    3. 15:45 ET hard close — never hold into close

Upgrade path:
    Replace _get_option_price() with a real OPRA data lookup once you
    have pulled historical options data from Databento.
"""

from dataclasses import dataclass, field
from datetime import time, date, timedelta, timezone
from zoneinfo import ZoneInfo
import datetime as dt
import math

from strategy.option_pricing import (
    price_option, get_iv_estimate, select_strike,
    days_to_nearest_expiry, OptionPrice,
)
from utils.logger import get_logger

logger = get_logger(__name__)

ET            = ZoneInfo("America/New_York")
MARKET_OPEN   = time(9, 30)
MARKET_CLOSE  = time(15, 45)


# ---------------------------------------------------------------------------
# Trade record — one entry per completed trade
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    date:           date
    direction:      str             # "CALL" or "PUT"
    strike:         float
    expiry_dte:     int
    entry_time:     str
    entry_underlying: float         # QQQ price at entry
    entry_option:   float           # option ask price paid
    contracts:      int
    premium_paid:   float           # total cash out = contracts × ask × 100
    exit_time:      str  = ""
    exit_option:    float = 0.0     # option price at exit
    pnl:            float = 0.0     # realised P&L in dollars
    exit_reason:    str  = ""
    entry_delta:    float = 0.0
    entry_iv:       float = 0.0


# ---------------------------------------------------------------------------
# Per-day state
# ---------------------------------------------------------------------------

@dataclass
class DayState:
    range_high:     float = 0.0
    range_low:      float = float("inf")
    range_set:      bool  = False
    range_width:    float = 0.0
    trade_fired:    bool  = False   # one trade per day
    position:       int   = 0       # number of contracts held
    entry_price:    float = 0.0     # option ask at entry
    stop_price:     float = 0.0     # option price that triggers stop
    target_price:   float = 0.0     # option price that triggers target
    direction:      str   = ""      # "CALL" or "PUT"
    strike:         float = 0.0
    entry_time:     str   = ""
    entry_underlying: float = 0.0
    daily_pnl:      float = 0.0
    entry_delta:    float = 0.0
    entry_iv:       float = 0.0


# ---------------------------------------------------------------------------
# Main strategy class
# ---------------------------------------------------------------------------

class ORBOptionsStrategy:
    """
    Opening Range Breakout strategy using options for position sizing.

    Args:
        symbol                  : underlying ticker (for logging/ordering)
        opening_range_minutes   : how many minutes after open to build range
        target_dte              : days to expiry for options bought
                                  0 = same-day (0DTE), 1 = next day, 7 = weekly
        strike_offset_pct       : how far OTM to go (0.0 = ATM, 0.005 = 0.5% OTM)
        strike_interval         : round strike to nearest N dollars (1.0 or 5.0)
        max_risk_per_trade      : max dollars to risk per trade (= max premium paid)
        stop_loss_pct           : exit if option loses this fraction of value (0.5 = 50%)
        target_mult             : exit if option gains this multiple of entry (2.0 = 2×)
        max_daily_loss          : stop trading for the day if cumulative loss hits this
        use_real_pricing        : False = Black-Scholes estimate
                                  True  = placeholder for OPRA data lookup
        spread_pct              : bid/ask half-spread as fraction of mid (BS only)
    """

    def __init__(
        self,
        symbol:                 str   = "QQQ",
        opening_range_minutes:  int   = 15,
        target_dte:             int   = 1,
        strike_offset_pct:      float = 0.0,      # ATM by default
        strike_interval:        float = 1.0,
        max_risk_per_trade:     float = 500.0,
        stop_loss_pct:          float = 0.50,      # exit at 50% loss
        target_mult:            float = 2.0,       # exit at 2× gain
        max_daily_loss:         float = 1000.0,
        use_real_pricing:       bool  = False,
        spread_pct:             float = 0.05,
    ):
        self.symbol               = symbol
        self.opening_range_minutes= opening_range_minutes
        self.target_dte           = target_dte
        self.strike_offset_pct    = strike_offset_pct
        self.strike_interval      = strike_interval
        self.max_risk_per_trade   = max_risk_per_trade
        self.stop_loss_pct        = stop_loss_pct
        self.target_mult          = target_mult
        self.max_daily_loss       = max_daily_loss
        self.use_real_pricing     = use_real_pricing
        self.spread_pct           = spread_pct

        self.state         = DayState()
        self._current_date = None
        self.trades:       list[TradeRecord] = []

    # -----------------------------------------------------------------------
    # Main tick handler
    # -----------------------------------------------------------------------

    def on_tick(self, record) -> dict | None:
        """
        Process one ohlcv-1s record.

        Returns a dict describing an option order to place, or None.
        Dict keys: symbol, option_type, strike, expiry_date,
                   side, contracts, limit_price, reason
        """
        bar_open  = record.open  / 1e9
        bar_high  = record.high  / 1e9
        bar_low   = record.low   / 1e9
        bar_close = record.close / 1e9

        ts_et    = _ns_to_et(record.ts_event)
        bar_time = ts_et.time()
        bar_date = ts_et.date()

        # ---- new day ----
        if bar_date != self._current_date:
            if self._current_date is not None:
                logger.info(
                    f"[{self._current_date}] Day end. "
                    f"Daily P&L: ${self.state.daily_pnl:+.2f}"
                )
            self._current_date = bar_date
            self.state = DayState()
            logger.info(
                f"[{bar_date}] New day. "
                f"ORB window: {self.opening_range_minutes} min | "
                f"DTE: {self.target_dte} | "
                f"Max risk/trade: ${self.max_risk_per_trade:.0f}"
            )

        if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
            return None

        if self.state.daily_pnl <= -abs(self.max_daily_loss):
            return None

        # ---- phase 1: build opening range ----
        if not self.state.range_set:
            return self._build_range(bar_time, bar_high, bar_low)

        # ---- EOD flatten ----
        if bar_time >= MARKET_CLOSE and self.state.position > 0:
            return self._exit(bar_close, bar_time, "EOD flatten")

        # ---- manage open position ----
        if self.state.position > 0:
            return self._manage_position(bar_close, bar_time)

        # ---- watch for breakout ----
        return self._check_breakout(bar_close, bar_date, bar_time)

    # -----------------------------------------------------------------------
    # Phase 1 — build opening range
    # -----------------------------------------------------------------------

    def _build_range(self, bar_time: time, bar_high: float, bar_low: float):
        range_end = _add_minutes(MARKET_OPEN, self.opening_range_minutes)

        if bar_time < range_end:
            self.state.range_high = max(self.state.range_high, bar_high)
            self.state.range_low  = min(self.state.range_low,  bar_low)
            return None

        self.state.range_set   = True
        self.state.range_width = self.state.range_high - self.state.range_low
        logger.info(
            f"  Range set: high={self.state.range_high:.2f}  "
            f"low={self.state.range_low:.2f}  "
            f"width={self.state.range_width:.2f}"
        )
        return None

    # -----------------------------------------------------------------------
    # Phase 2 — check for breakout, size and price the option
    # -----------------------------------------------------------------------

    def _check_breakout(
        self, bar_close: float, bar_date: date, bar_time: time
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

        # ---- price the option ----
        opt = self._get_option_price(bar_close, direction, bar_date)
        if opt is None:
            return None

        # ---- size: how many contracts can we buy within max_risk? ----
        cost_per_contract = opt.ask * 100.0
        if cost_per_contract <= 0:
            logger.warning("Option ask is zero — skipping trade.")
            return None

        contracts = int(self.max_risk_per_trade / cost_per_contract)
        if contracts < 1:
            logger.warning(
                f"Option too expensive for risk budget "
                f"(ask=${opt.ask:.2f}, cost/contract=${cost_per_contract:.0f}, "
                f"budget=${self.max_risk_per_trade:.0f}) — skipping."
            )
            return None

        premium_paid  = contracts * cost_per_contract
        stop_price    = opt.ask * (1.0 - self.stop_loss_pct)
        target_price  = opt.ask * self.target_mult

        # ---- record state ----
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

        expiry_date = _get_expiry_date(bar_date, self.target_dte)

        logger.info(
            f"  BREAKOUT {direction} @ underlying={bar_close:.2f} | "
            f"strike={opt.strike:.1f}  ask=${opt.ask:.2f}  "
            f"delta={opt.delta:.3f}  IV={opt.iv:.1%} | "
            f"contracts={contracts}  risk=${premium_paid:.0f} | "
            f"stop=${stop_price:.2f}  target=${target_price:.2f}"
        )

        return {
            "symbol":      self.symbol,
            "option_type": direction,
            "strike":      opt.strike,
            "expiry_date": expiry_date,
            "side":        "BUY_OPEN",
            "contracts":   contracts,
            "limit_price": round(opt.ask + 0.05, 2),   # small buffer above ask
            "reason":      f"ORB {direction} breakout",
        }

    # -----------------------------------------------------------------------
    # Phase 3 — manage open position
    # -----------------------------------------------------------------------

    def _manage_position(
        self, bar_close: float, bar_time: time
    ) -> dict | None:
        # Re-price the option at current underlying
        opt = self._get_option_price(
            bar_close,
            self.state.direction,
            self._current_date,
        )
        if opt is None:
            return None

        current_price = opt.price

        # Stop loss
        if current_price <= self.state.stop_price:
            return self._exit(bar_close, bar_time, "Stop loss", current_price)

        # Take profit
        if current_price >= self.state.target_price:
            return self._exit(bar_close, bar_time, "Take profit", current_price)

        return None

    # -----------------------------------------------------------------------
    # Exit — record trade, update P&L, return order dict
    # -----------------------------------------------------------------------

    def _exit(
        self,
        underlying: float,
        bar_time:   time,
        reason:     str,
        exit_option_price: float | None = None,
    ) -> dict:
        if exit_option_price is None:
            opt = self._get_option_price(
                underlying, self.state.direction, self._current_date
            )
            exit_option_price = opt.price if opt else self.state.entry_price * 0.5

        pnl = (
            (exit_option_price - self.state.entry_price)
            * self.state.position
            * 100.0
        )
        self.state.daily_pnl += pnl

        logger.info(
            f"  EXIT [{reason}] "
            f"entry=${self.state.entry_price:.2f}  "
            f"exit=${exit_option_price:.2f}  "
            f"contracts={self.state.position}  "
            f"P&L=${pnl:+.2f}  "
            f"daily=${self.state.daily_pnl:+.2f}"
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
            "expiry_date": _get_expiry_date(self._current_date, self.target_dte),
            "side":        "SELL_CLOSE",
            "contracts":   self.state.position,
            "limit_price": round(exit_option_price - 0.05, 2),
            "reason":      reason,
        }

    # -----------------------------------------------------------------------
    # Option pricer — swap this for real OPRA data when available
    # -----------------------------------------------------------------------

    def _get_option_price(
        self,
        spot:       float,
        direction:  str,
        bar_date:   date,
    ) -> OptionPrice | None:
        """
        Return an OptionPrice for the target strike.

        Currently uses Black-Scholes with a year-appropriate IV estimate.
        To upgrade to real OPRA data:
            1. Load your OPRA dataset for the current date
            2. Look up the contract matching (strike, expiry, option_type)
            3. Return an OptionPrice built from real bid/ask/greeks
        """
        if self.use_real_pricing:
            # --- OPRA data hook (not yet implemented) ---
            # Implement your real data lookup here.
            # Return None to skip the trade if data is unavailable.
            raise NotImplementedError(
                "Real OPRA pricing not yet implemented. "
                "Set use_real_pricing=False to use Black-Scholes estimates."
            )

        # Black-Scholes estimate
        strike = select_strike(
            spot,
            offset_pct = self.strike_offset_pct if direction == "CALL" else -self.strike_offset_pct,
            interval   = self.strike_interval,
        )
        T     = days_to_nearest_expiry(self.target_dte)
        sigma = get_iv_estimate(bar_date.year)

        return price_option(
            S           = spot,
            K           = strike,
            T           = T,
            sigma       = sigma,
            option_type = direction,
            spread_pct  = self.spread_pct,
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _ns_to_et(ts_ns: int):
    import datetime as dt
    ts_s   = ts_ns / 1e9
    dt_utc = dt.datetime.fromtimestamp(ts_s, tz=timezone.utc)
    return dt_utc.astimezone(ET)


def _add_minutes(t: time, minutes: int) -> time:
    import datetime as dt
    base = dt.datetime(2000, 1, 1, t.hour, t.minute, t.second)
    base += timedelta(minutes=minutes)
    return base.time()


def _get_expiry_date(trade_date: date, dte: int) -> date:
    """
    Calculate the option expiry date from trade date + DTE.
    For 0DTE: same day.
    For 1+ DTE: skip weekends (simplified — does not skip holidays).
    """
    if dte == 0:
        return trade_date
    expiry = trade_date
    days_added = 0
    while days_added < dte:
        expiry += timedelta(days=1)
        if expiry.weekday() < 5:    # Mon–Fri
            days_added += 1
    return expiry
