"""
backtest/run_orb_options.py — Full backtest runner for the ORB options strategy.

Usage (from project root):
    python -m backtest.run_orb_options

Output:
    - Console summary: P&L, win rate, avg win/loss, Sharpe, max drawdown
    - backtest/results/orb_options_trades.csv   — every trade
    - backtest/results/orb_options_equity.csv   — daily equity curve

Configuration is pulled from config.py. To run parameter sweeps, see
the sweep_parameters() function at the bottom of this file.
"""

import csv
import os
import math
from datetime import date
from collections import defaultdict

import databento as db

from config import Config
from strategy.orb_options import ORBOptionsStrategy, TradeRecord
from utils.logger import get_logger

logger = get_logger(__name__)

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    opening_range_minutes: int   = 15,
    target_dte:            int   = 1,
    strike_offset_pct:     float = 0.0,
    max_risk_per_trade:    float = 500.0,
    stop_loss_pct:         float = 0.50,
    target_mult:           float = 2.0,
    max_daily_loss:        float = 1000.0,
    label:                 str   = "default",
) -> dict:
    """
    Run one full backtest pass over the historical data.
    Returns a summary dict with all key performance metrics.
    """
    logger.info("=" * 65)
    logger.info(f"BACKTEST: ORB Options  [{label}]")
    logger.info(
        f"  ORB window={opening_range_minutes}m  DTE={target_dte}  "
        f"offset={strike_offset_pct:.1%}  "
        f"risk/trade=${max_risk_per_trade:.0f}  "
        f"stop={stop_loss_pct:.0%}  target={target_mult:.1f}×"
    )
    logger.info("=" * 65)

    strategy = ORBOptionsStrategy(
        symbol                = Config.SYMBOLS[0],
        opening_range_minutes = opening_range_minutes,
        target_dte            = target_dte,
        strike_offset_pct     = strike_offset_pct,
        max_risk_per_trade    = max_risk_per_trade,
        stop_loss_pct         = stop_loss_pct,
        target_mult           = target_mult,
        max_daily_loss        = max_daily_loss,
        use_real_pricing      = False,
    )

    store = db.DBNStore.from_file(Config.HISTORICAL_DATA_PATH)

    bars_processed = 0
    for record in store:
        bars_processed += 1
        strategy.on_tick(record)

    logger.info(f"Bars processed: {bars_processed:,}")

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

    pnls      = [t.pnl for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)

    # Daily P&L for Sharpe and drawdown
    daily_pnl: dict[date, float] = defaultdict(float)
    for t in trades:
        daily_pnl[t.date] += t.pnl

    daily_returns = list(daily_pnl.values())

    # Sharpe (annualised, assumes 252 trading days)
    if len(daily_returns) > 1:
        mean_r  = sum(daily_returns) / len(daily_returns)
        var_r   = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_r   = math.sqrt(var_r) if var_r > 0 else 1e-9
        sharpe  = (mean_r / std_r) * math.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown on cumulative equity curve
    equity   = 0.0
    peak     = 0.0
    max_dd   = 0.0
    equity_curve = []
    for d in sorted(daily_pnl):
        equity += daily_pnl[d]
        equity_curve.append((d, equity))
        peak    = max(peak, equity)
        dd      = peak - equity
        max_dd  = max(max_dd, dd)

    # Premium stats
    total_premium = sum(t.premium_paid for t in trades)
    avg_dte       = sum(t.expiry_dte   for t in trades) / len(trades)

    return {
        "label":            label,
        "total_trades":     len(trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         len(wins) / len(trades) if trades else 0,
        "total_pnl":        round(total_pnl, 2),
        "avg_win":          round(sum(wins)   / len(wins),   2) if wins   else 0,
        "avg_loss":         round(sum(losses) / len(losses), 2) if losses else 0,
        "largest_win":      round(max(pnls), 2),
        "largest_loss":     round(min(pnls), 2),
        "profit_factor":    round(sum(wins) / abs(sum(losses)), 2) if losses else float("inf"),
        "sharpe":           round(sharpe, 2),
        "max_drawdown":     round(max_dd, 2),
        "total_premium":    round(total_premium, 2),
        "avg_dte":          round(avg_dte, 1),
        "avg_delta_entry":  round(sum(t.entry_delta for t in trades) / len(trades), 3),
        "avg_iv_entry":     round(sum(t.entry_iv    for t in trades) / len(trades), 3),
        "equity_curve":     equity_curve,
        "trading_days":     len(daily_pnl),
    }


def _print_summary(s: dict):
    if s["total_trades"] == 0:
        logger.info("No trades generated.")
        return

    logger.info("")
    logger.info("─" * 50)
    logger.info(f"  RESULTS [{s['label']}]")
    logger.info("─" * 50)
    logger.info(f"  Trades       : {s['total_trades']}  "
                f"({s['wins']}W / {s['losses']}L)  "
                f"Win rate: {s['win_rate']:.1%}")
    logger.info(f"  Total P&L    : ${s['total_pnl']:+,.2f}")
    logger.info(f"  Avg win      : ${s['avg_win']:+,.2f}   "
                f"Avg loss: ${s['avg_loss']:+,.2f}")
    logger.info(f"  Largest win  : ${s['largest_win']:+,.2f}   "
                f"Largest loss: ${s['largest_loss']:+,.2f}")
    logger.info(f"  Profit factor: {s['profit_factor']:.2f}")
    logger.info(f"  Sharpe ratio : {s['sharpe']:.2f}  "
                f"(annualised, daily returns)")
    logger.info(f"  Max drawdown : ${s['max_drawdown']:,.2f}")
    logger.info(f"  Total premium: ${s['total_premium']:,.2f}")
    logger.info(f"  Trading days : {s['trading_days']}")
    logger.info(f"  Avg DTE      : {s['avg_dte']}")
    logger.info(f"  Avg entry Δ  : {s['avg_delta_entry']:.3f}   "
                f"Avg entry IV: {s['avg_iv_entry']:.1%}")
    logger.info("─" * 50)
    logger.info("")


# ---------------------------------------------------------------------------
# Save results to CSV
# ---------------------------------------------------------------------------

def _save_results(trades: list[TradeRecord], summary: dict, label: str):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe_label = label.replace(" ", "_").replace("/", "-")

    # -- trades CSV --
    trades_path = os.path.join(RESULTS_DIR, f"{safe_label}_trades.csv")
    with open(trades_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "direction", "strike", "expiry_dte",
            "entry_time", "entry_underlying", "entry_option",
            "contracts", "premium_paid",
            "exit_time", "exit_option", "pnl", "exit_reason",
            "entry_delta", "entry_iv",
        ])
        for t in trades:
            writer.writerow([
                t.date, t.direction, t.strike, t.expiry_dte,
                t.entry_time, t.entry_underlying, t.entry_option,
                t.contracts, t.premium_paid,
                t.exit_time, t.exit_option, t.pnl, t.exit_reason,
                t.entry_delta, t.entry_iv,
            ])
    logger.info(f"Trades saved  → {trades_path}")

    # -- equity curve CSV --
    equity_path = os.path.join(RESULTS_DIR, f"{safe_label}_equity.csv")
    with open(equity_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "cumulative_pnl"])
        for d, pnl in summary.get("equity_curve", []):
            writer.writerow([d, round(pnl, 2)])
    logger.info(f"Equity curve  → {equity_path}")


# ---------------------------------------------------------------------------
# Parameter sweep — run multiple configs, print comparison table
# ---------------------------------------------------------------------------

def sweep_parameters():
    """
    Run a grid of parameter combinations and print a ranked comparison.

    Useful for finding which ORB window / DTE / stop combination works best.
    Edit the grids below to suit your exploration.
    """
    configs = [
        # (opening_range_minutes, target_dte, stop_loss_pct, target_mult, label)
        (5,  0, 0.50, 2.0, "ORB5m_0DTE_50stop_2x"),
        (5,  1, 0.50, 2.0, "ORB5m_1DTE_50stop_2x"),
        (15, 0, 0.50, 2.0, "ORB15m_0DTE_50stop_2x"),
        (15, 1, 0.50, 2.0, "ORB15m_1DTE_50stop_2x"),
        (15, 1, 0.50, 3.0, "ORB15m_1DTE_50stop_3x"),
        (15, 1, 0.35, 2.0, "ORB15m_1DTE_35stop_2x"),
        (30, 1, 0.50, 2.0, "ORB30m_1DTE_50stop_2x"),
        (30, 7, 0.50, 2.0, "ORB30m_7DTE_50stop_2x"),
    ]

    results = []
    for (orb_min, dte, stop, target, lbl) in configs:
        summary = run_backtest(
            opening_range_minutes = orb_min,
            target_dte            = dte,
            stop_loss_pct         = stop,
            target_mult           = target,
            label                 = lbl,
        )
        results.append(summary)

    # Print ranked comparison table
    print("\n" + "=" * 90)
    print(f"{'Label':<30} {'Trades':>6} {'WinRate':>8} {'TotalPnL':>10} "
          f"{'Sharpe':>7} {'MaxDD':>9} {'PF':>6}")
    print("=" * 90)
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
            f"{r['profit_factor']:>6.2f}"
        )
    print("=" * 90)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        sweep_parameters()
    else:
        run_backtest(
            opening_range_minutes = Config.ORB_OPENING_RANGE_MINUTES,
            target_dte            = Config.ORB_TARGET_DTE,
            strike_offset_pct     = Config.ORB_STRIKE_OFFSET_PCT,
            max_risk_per_trade    = Config.MAX_RISK_PER_TRADE,
            stop_loss_pct         = Config.ORB_STOP_LOSS_PCT,
            target_mult           = Config.ORB_TARGET_MULT,
            max_daily_loss        = Config.MAX_DAILY_LOSS,
            label                 = "single_run",
        )
