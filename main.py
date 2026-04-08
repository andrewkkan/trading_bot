"""
main.py — Entry point for the trading bot.

Run modes:
    python main.py backtest          — ORB equity backtest (shares)
    python main.py backtest_options  — ORB options backtest (Black-Scholes)
    python main.py sweep             — parameter sweep across ORB options configs
    python main.py paper             — live feed, log orders but don't send
    python main.py live              — live feed + real E*TRADE execution
"""

import sys
import asyncio
import databento as db

from config import Config
from strategy.orb import ORBStrategy
from strategy.orb_options import ORBOptionsStrategy
from broker.etrade import ETradeClient, OptionOrder
from utils.logger import get_logger
from datetime import date as Date

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Equity backtest (original ORB on shares)
# ---------------------------------------------------------------------------

def run_equity_backtest():
    from backtest.run_orb_equity import run_backtest
    run_backtest(
        quantity              = 10,
        opening_range_minutes = Config.ORB_OPENING_RANGE_MINUTES,
        rr_ratio              = Config.ORB_RR_RATIO,
        max_daily_loss        = Config.MAX_DAILY_LOSS,
        max_window_multiplier = Config.ORB_MAX_WINDOW_MULTIPLIER,
        min_range_pct         = Config.ORB_MIN_RANGE_PCT,
        rolling_lookback_days = Config.ORB_ROLLING_LOOKBACK_DAYS,
        min_bootstrap_days    = Config.ORB_MIN_BOOTSTRAP_DAYS,
        breakout_bars         = Config.ORB_BREAKOUT_BARS,
        retest_bars           = Config.ORB_RETEST_BARS,
        reconfirm_bars        = Config.ORB_RECONFIRM_BARS,
        min_hold_minutes      = Config.ORB_MIN_HOLD_MINUTES,
        gap_lookback_days     = Config.GAP_LOOKBACK_DAYS,
        gap_none_threshold    = Config.GAP_NONE_THRESHOLD,
        vol_lookback_days     = Config.VOL_LOOKBACK_DAYS,
        vol_bars_to_track     = Config.VOL_BARS_TO_TRACK,
        label                 = "orb_equity",
    )


# ---------------------------------------------------------------------------
# Options backtest
# ---------------------------------------------------------------------------

def run_options_backtest():
    from backtest.run_orb_options import run_backtest
    run_backtest(
        opening_range_minutes = Config.ORB_OPENING_RANGE_MINUTES,
        rr_ratio              = Config.ORB_RR_RATIO,
        target_dte            = Config.ORB_TARGET_DTE,
        strike_offset_pct     = Config.ORB_STRIKE_OFFSET_PCT,
        max_risk_per_trade    = Config.MAX_RISK_PER_TRADE,
        max_daily_loss        = Config.MAX_DAILY_LOSS,
        max_window_multiplier = Config.ORB_MAX_WINDOW_MULTIPLIER,
        min_range_pct         = Config.ORB_MIN_RANGE_PCT,
        rolling_lookback_days = Config.ORB_ROLLING_LOOKBACK_DAYS,
        min_bootstrap_days    = Config.ORB_MIN_BOOTSTRAP_DAYS,
        breakout_bars         = Config.ORB_BREAKOUT_BARS,
        retest_bars           = Config.ORB_RETEST_BARS,
        reconfirm_bars        = Config.ORB_RECONFIRM_BARS,
        min_hold_minutes      = Config.ORB_MIN_HOLD_MINUTES,
        vol_lookback_days     = Config.VOL_LOOKBACK_DAYS,
        vol_bars_to_track     = Config.VOL_BARS_TO_TRACK,
        label                 = "orb_options",
    )


def run_equity_sweep():
    from backtest.run_orb_equity import sweep_parameters
    sweep_parameters()


def run_parameter_sweep():
    from backtest.run_orb_options import sweep_parameters
    sweep_parameters()


# ---------------------------------------------------------------------------
# Live / paper trading
# ---------------------------------------------------------------------------

async def run_live(paper: bool = False):
    mode = "PAPER" if paper else "LIVE"
    logger.info(f"MODE: {mode}")

    strategy = ORBOptionsStrategy(
        symbol                = Config.SYMBOLS[0],
        opening_range_minutes = Config.ORB_OPENING_RANGE_MINUTES,
        rr_ratio              = Config.ORB_RR_RATIO,
        target_dte            = Config.ORB_TARGET_DTE,
        strike_offset_pct     = Config.ORB_STRIKE_OFFSET_PCT,
        max_risk_per_trade    = Config.MAX_RISK_PER_TRADE,
        max_daily_loss        = Config.MAX_DAILY_LOSS,
        max_window_multiplier = Config.ORB_MAX_WINDOW_MULTIPLIER,
        min_range_pct         = Config.ORB_MIN_RANGE_PCT,
        rolling_lookback_days = Config.ORB_ROLLING_LOOKBACK_DAYS,
        min_bootstrap_days    = Config.ORB_MIN_BOOTSTRAP_DAYS,
        breakout_bars         = Config.ORB_BREAKOUT_BARS,
        retest_bars           = Config.ORB_RETEST_BARS,
        reconfirm_bars        = Config.ORB_RECONFIRM_BARS,
        min_hold_minutes      = Config.ORB_MIN_HOLD_MINUTES,
        gap_lookback_days     = Config.GAP_LOOKBACK_DAYS,
        gap_none_threshold    = Config.GAP_NONE_THRESHOLD,
        vol_lookback_days     = Config.VOL_LOOKBACK_DAYS,
        vol_bars_to_track     = Config.VOL_BARS_TO_TRACK,
        use_real_pricing      = False,
    )

    broker = None
    if not paper:
        broker = ETradeClient(
            consumer_key    = Config.ETRADE_CONSUMER_KEY,
            consumer_secret = Config.ETRADE_CONSUMER_SECRET,
            sandbox         = Config.SANDBOX_MODE,
            account_id      = Config.ETRADE_ACCOUNT_ID,
        )
        broker.authenticate()

    client = db.Live(key=Config.DATABENTO_API_KEY)
    client.subscribe(dataset=Config.DATASET, schema=Config.SCHEMA, symbols=Config.SYMBOLS)

    async for record in client:
        order_dict = strategy.on_tick(record)
        if order_dict:
            if paper:
                logger.info(f"[PAPER] {order_dict}")
            else:
                opt_order = OptionOrder(
                    symbol      = order_dict["symbol"],
                    expiry      = order_dict["expiry_date"],
                    option_type = order_dict["option_type"],
                    strike      = order_dict["strike"],
                    side        = order_dict["side"],
                    quantity    = order_dict["contracts"],
                    order_type  = "LIMIT",
                    limit_price = order_dict["limit_price"],
                    reason      = order_dict["reason"],
                )
                result = broker.place_option_order(opt_order)
                logger.info(f"Order placed: {result}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest_options"

    if   mode == "backtest":          run_equity_backtest()
    elif mode == "backtest_options":  run_options_backtest()
    elif mode == "sweep":             run_parameter_sweep()
    elif mode == "sweep_equity":      run_equity_sweep()
    elif mode == "paper":             asyncio.run(run_live(paper=True))
    elif mode == "live":              asyncio.run(run_live(paper=False))
    else:
        print("Usage: python main.py [backtest|backtest_options|sweep|sweep_equity|paper|live]")
        sys.exit(1)
