"""
backtest/run_orb_equity.py — Full backtest runner for the ORB equity strategy.

Usage (from project root):
    python -m backtest.run_orb_equity
    python -m backtest.run_orb_equity sweep

Output:
    - Console summary: P&L, win rate, avg win/loss, Sharpe, max drawdown
    - backtest/results/<label>_trades.csv  — every trade
    - backtest/results/<label>_equity.csv  — daily equity curve
"""

import csv
import os
import math
from datetime import date, datetime
from collections import defaultdict

import databento as db

from config import Config
from strategy.orb import ORBStrategy, TradeRecord
from utils.logger import get_logger

logger = get_logger(__name__)

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbol:                str   = None,
    quantity:              int   = 10,
    opening_range_minutes: int   = 15,
    rr_ratio:              float = 2.0,
    max_daily_loss:        float = 1000.0,
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
    slippage:              float = 0.01,
    start_date:            date  | None = None,
    end_date:              date  | None = None,
    dates:                 list  | None = None,
    label:                 str   = "default",
) -> dict:
    """
    Run one full backtest pass over the historical data file.
    Returns a summary dict with all key performance metrics.
    """
    if symbol is None:
        symbol = Config.SYMBOLS[0]

    logger.info("=" * 65)
    logger.info(f"BACKTEST: ORB Equity  [{label}]")
    logger.info(
        f"  symbol={symbol}  qty={quantity}  "
        f"ORB={opening_range_minutes}m  RR={rr_ratio}×  "
        f"breakout={breakout_bars}  retest={retest_bars}  "
        f"reconfirm={reconfirm_bars}  min_hold={min_hold_minutes}m"
    )
    logger.info("=" * 65)

    strategy = ORBStrategy(
        symbol                = symbol,
        quantity              = quantity,
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
        slippage              = slippage,
    )

    # Normalise date filter inputs
    dates_set = set(dates) if dates else None

    store = db.DBNStore.from_file(Config.HISTORICAL_DATA_PATH)

    bars_processed = bars_skipped = 0
    for record in store:
        bar_date = _record_date(record)

        # Date range filter
        if start_date and bar_date < start_date:
            bars_skipped += 1
            continue
        if end_date and bar_date > end_date:
            bars_skipped += 1
            continue
        if dates_set and bar_date not in dates_set:
            bars_skipped += 1
            continue

        bars_processed += 1
        strategy.on_tick(record)

    logger.info(f"Bars processed: {bars_processed:,}  skipped: {bars_skipped:,}")

    summary = _compute_summary(strategy.trades, label)
    _print_summary(summary)
    _save_results(strategy.trades, summary, label)

    return summary


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _compute_summary(trades: list[TradeRecord], label: str) -> dict:
    if not trades:
        return {"label": label, "total_trades": 0, "total_pnl": 0.0}

    pnls   = [t.pnl for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Daily P&L for Sharpe and drawdown
    daily_pnl: dict[date, float] = defaultdict(float)
    for t in trades:
        daily_pnl[t.date] += t.pnl

    daily_returns = list(daily_pnl.values())

    # Sharpe (annualised, 252 trading days)
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        var_r  = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_r  = math.sqrt(var_r) if var_r > 0 else 1e-9
        sharpe = (mean_r / std_r) * math.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    equity_curve = []
    for d in sorted(daily_pnl):
        equity += daily_pnl[d]
        equity_curve.append((d, equity))
        peak   = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Direction breakdown
    longs  = [t for t in trades if t.direction == "LONG"]
    shorts = [t for t in trades if t.direction == "SHORT"]

    # Exit reason breakdown
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t.exit_reason] += 1

    # Gap signal breakdown
    gap_up_trades   = [t for t in trades if t.gap_direction == "UP"]
    gap_down_trades = [t for t in trades if t.gap_direction == "DOWN"]
    gap_none_trades = [t for t in trades if t.gap_direction in ("NONE", "N/A")]

    return {
        "label":          label,
        "total_trades":   len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       len(wins) / len(trades),
        "total_pnl":      round(sum(pnls), 2),
        "avg_win":        round(sum(wins)   / len(wins),   2) if wins   else 0,
        "avg_loss":       round(sum(losses) / len(losses), 2) if losses else 0,
        "largest_win":    round(max(pnls), 2),
        "largest_loss":   round(min(pnls), 2),
        "profit_factor":  round(sum(wins) / abs(sum(losses)), 2) if losses else float("inf"),
        "sharpe":         round(sharpe, 2),
        "max_drawdown":   round(max_dd, 2),
        "trading_days":   len(daily_pnl),
        "equity_curve":   equity_curve,
        # Direction
        "long_trades":    len(longs),
        "short_trades":   len(shorts),
        "long_pnl":       round(sum(t.pnl for t in longs),  2),
        "short_pnl":      round(sum(t.pnl for t in shorts), 2),
        # Exit reasons
        "stops":          by_reason.get("Stop loss",    0),
        "targets":        by_reason.get("Take profit",  0),
        "eod_closes":     by_reason.get("EOD flatten",  0),
        # Gap context
        "gap_up_trades":  len(gap_up_trades),
        "gap_down_trades":len(gap_down_trades),
        "gap_none_trades":len(gap_none_trades),
        "gap_up_pnl":     round(sum(t.pnl for t in gap_up_trades),   2),
        "gap_down_pnl":   round(sum(t.pnl for t in gap_down_trades), 2),
    }


def _print_summary(s: dict):
    if s["total_trades"] == 0:
        logger.info("No trades generated.")
        return

    logger.info("")
    logger.info("─" * 55)
    logger.info(f"  RESULTS [{s['label']}]")
    logger.info("─" * 55)
    logger.info(
        f"  Trades       : {s['total_trades']}  "
        f"({s['wins']}W / {s['losses']}L)  "
        f"Win rate: {s['win_rate']:.1%}"
    )
    logger.info(f"  Total P&L    : ${s['total_pnl']:+,.2f}")
    logger.info(
        f"  Avg win      : ${s['avg_win']:+,.2f}   "
        f"Avg loss: ${s['avg_loss']:+,.2f}"
    )
    logger.info(
        f"  Largest win  : ${s['largest_win']:+,.2f}   "
        f"Largest loss: ${s['largest_loss']:+,.2f}"
    )
    logger.info(f"  Profit factor: {s['profit_factor']:.2f}")
    logger.info(
        f"  Sharpe ratio : {s['sharpe']:.2f}  "
        f"(annualised, daily returns)"
    )
    logger.info(f"  Max drawdown : ${s['max_drawdown']:,.2f}")
    logger.info(f"  Trading days : {s['trading_days']}")
    logger.info(
        f"  Direction    : {s['long_trades']}L (${s['long_pnl']:+,.2f})  "
        f"{s['short_trades']}S (${s['short_pnl']:+,.2f})"
    )
    logger.info(
        f"  Exits        : {s['stops']} stops  "
        f"{s['targets']} targets  "
        f"{s['eod_closes']} EOD"
    )
    logger.info(
        f"  Gap context  : "
        f"UP {s['gap_up_trades']} (${s['gap_up_pnl']:+,.2f})  "
        f"DOWN {s['gap_down_trades']} (${s['gap_down_pnl']:+,.2f})  "
        f"NONE {s['gap_none_trades']}"
    )
    logger.info("─" * 55)
    logger.info("")


# ---------------------------------------------------------------------------
# Save results to CSV
# ---------------------------------------------------------------------------

def _save_results(trades: list[TradeRecord], summary: dict, label: str):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe = label.replace(" ", "_").replace("/", "-")

    # Trades CSV
    trades_path = os.path.join(RESULTS_DIR, f"{safe}_trades.csv")
    with open(trades_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "direction",
            "entry_time", "exit_time",
            "entry_price", "exit_price",
            "quantity", "pnl", "exit_reason",
            "range_high", "range_low", "range_width",
            "gap_direction", "gap_pct", "vol_rel",
        ])
        for t in trades:
            writer.writerow([
                t.date, t.direction,
                t.entry_time, t.exit_time,
                t.entry_price, t.exit_price,
                t.quantity, t.pnl, t.exit_reason,
                t.range_high, t.range_low, t.range_width,
                t.gap_direction, t.gap_pct, t.vol_rel,
            ])
    logger.info(f"Trades saved  → {trades_path}")

    # Equity curve CSV
    equity_path = os.path.join(RESULTS_DIR, f"{safe}_equity.csv")
    with open(equity_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "cumulative_pnl"])
        for d, pnl in summary.get("equity_curve", []):
            writer.writerow([d, round(pnl, 2)])
    logger.info(f"Equity curve  → {equity_path}")


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def sweep_parameters():
    """
    Run a grid of parameter combinations and print a ranked comparison.
    Edit configs below to suit your exploration.
    """
    configs = [
        # (orb_min, rr, qty, breakout, retest, reconfirm, hold, label)
        (15, 2.0, 10, 3, 3, 3, 30, "ORB15m_RR2_b3r3"),
        (15, 3.0, 10, 3, 3, 3, 30, "ORB15m_RR3_b3r3"),
        (15, 2.0, 10, 5, 3, 3, 30, "ORB15m_RR2_b5r3"),
        (15, 2.0, 10, 3, 5, 3, 30, "ORB15m_RR2_b3r5"),
        (30, 2.0, 10, 3, 3, 3, 30, "ORB30m_RR2_b3r3"),
        (30, 3.0, 10, 3, 3, 3, 30, "ORB30m_RR3_b3r3"),
        (5,  2.0, 10, 3, 3, 3, 30, "ORB5m_RR2_b3r3"),
        (5,  3.0, 10, 3, 3, 3, 30, "ORB5m_RR3_b3r3"),
    ]

    results = []
    for (orb_min, rr, qty, bo, rt, rc, hold, lbl) in configs:
        summary = run_backtest(
            quantity              = qty,
            opening_range_minutes = orb_min,
            rr_ratio              = rr,
            breakout_bars         = bo,
            retest_bars           = rt,
            reconfirm_bars        = rc,
            min_hold_minutes      = hold,
            label                 = lbl,
        )
        results.append(summary)

    print("\n" + "=" * 95)
    print(
        f"{'Label':<30} {'Trades':>6} {'WinRate':>8} "
        f"{'TotalPnL':>10} {'Sharpe':>7} {'MaxDD':>9} "
        f"{'PF':>6} {'L/S':>8}"
    )
    print("=" * 95)
    for r in sorted(results, key=lambda x: x.get("total_pnl", 0), reverse=True):
        if r["total_trades"] == 0:
            continue
        print(
            f"{r['label']:<30} "
            f"{r['total_trades']:>6} "
            f"{r['win_rate']:>8.1%} "
            f"{r['total_pnl']:>+10,.2f} "
            f"{r['sharpe']:>7.2f} "
            f"{r['max_drawdown']:>9,.2f} "
            f"{r['profit_factor']:>6.2f} "
            f"{r['long_trades']:>3}L/{r['short_trades']:<3}S"
        )
    print("=" * 95)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _record_date(record) -> date:
    """Extract the ET date from a Databento ohlcv record."""
    from strategy.utils import ns_to_et
    return ns_to_et(record.ts_event).date()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        sweep_parameters()
    else:
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
            slippage              = Config.SLIPPAGE,
            label                 = "single_run",
        )
