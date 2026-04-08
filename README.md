# trading_bot

A real-time trading bot that streams market data via Databento and executes
orders through E*TRADE. Implements an Opening Range Breakout (ORB) strategy
with retest confirmation, adaptive range validation, and support for both
equity (shares) and options execution.

---

## Project structure

```
trading_bot/
├── main.py                          # Entry point — all run modes
├── config.py                        # All settings and credentials
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── strategy/
│   ├── orb_base.py                  # Abstract base — all price logic (edit here first)
│   ├── orb.py                       # Equity execution + TradeRecord
│   ├── orb_options.py               # Options execution + TradeRecord
│   ├── retest_engine.py             # Breakout → retest → entry state machine (both directions)
│   ├── range_builder.py             # Adaptive opening range with rolling avg width validation
│   ├── gap_detector.py              # Overnight gap vs prior close + prior high/low
│   ├── volume_evaluator.py          # Volume evaluation at breakout confirmation
│   ├── option_pricing.py            # Black-Scholes pricer + greeks (B-S placeholder)
│   └── utils.py                     # Shared time/date utilities (ET conversion etc.)
│
├── broker/
│   ├── etrade.py                    # E*TRADE OAuth + equity + option order placement
│   └── option_examples.py           # Runnable option order examples
│
├── backtest/
│   ├── result_store.py              # SQLite result store (events, trades, daily_summary)
│   ├── run_orb_equity.py            # Equity backtest runner + parameter sweep
│   └── run_orb_options.py           # Options backtest runner + parameter sweep
│
├── data/
│   └── feed.py                      # Databento live stream wrapper
│
├── historical_data/                 # Local DBN files (git-ignored)
│   └── QQQ_ohlcv_1s.dbn.zst        # Downloaded from Databento
│
└── utils/
    └── logger.py                    # Shared logging
```

---

## Strategy architecture

All ORB variants inherit from `ORBBase`. The base class owns every
price-level decision. Subclasses implement execution only.

```
ORBBase  (orb_base.py)
├── ORBStrategy        (orb.py)          — trades shares
└── ORBOptionsStrategy (orb_options.py)  — trades options
```

**Edit `orb_base.py`** to change signal logic (range, retest, stop, target).
**Edit `orb.py`** to change equity order construction.
**Edit `orb_options.py`** to change option selection, sizing, or OPRA pricing.

### Signal flow per bar

```
1. RangeBuilder   — adaptive opening range with rolling avg width validation
2. GapDetector    — overnight gap vs prior close and prior high/low
3. VolumeEvaluator— per-bar volume tracking; evaluated at entry
4. RetestEngine   — breakout → retest → reconfirmation state machine
                    (both LONG and SHORT directions, independent state)
5. _on_entry()    — subclass builds and returns the order
```

### RetestEngine state machine

```
For each direction (LONG / SHORT) independently:

  IDLE
    → breakout_bars consecutive closes outside range boundary
  BREAKOUT CONFIRMED
    → retest_bars consecutive closes inside the window
      OR opposing breakout confirmed (implicit credit via window expansion)
  RETEST CREDITED
    → reconfirm_bars consecutive closes outside boundary again
  ENTRY

Window expansion: when the opposing breakout is confirmed, the boundary
on the first-confirmed direction's side expands to its breakout extreme,
and that direction's retest is credited implicitly.

No trade fires if price never retests. Skip = high probability only.
```

---

## Setup

```bash
# 1. Copy and fill in credentials
cp .env.example .env
nano .env

# 2. Build and run with Docker (recommended)
docker compose up --build

# 3. Or run directly
pip install -r requirements.txt
python main.py backtest
```

---

## Run modes

| Mode | Command | Executes | Needs credentials |
|---|---|---|---|
| Equity backtest | `python main.py backtest` | `ORBStrategy` — trades shares | Databento only |
| Equity sweep | `python main.py sweep_equity` | `ORBStrategy` — 8 param combos | Databento only |
| Options backtest | `python main.py backtest_options` | `ORBOptionsStrategy` — trades options | Databento only |
| Options sweep | `python main.py sweep` | `ORBOptionsStrategy` — 8 param combos | Databento only |
| Paper trading | `python main.py paper` | `ORBOptionsStrategy` — live feed, orders logged not sent | Databento + E*TRADE |
| Live trading | `python main.py live` | `ORBOptionsStrategy` — live feed, real option orders via E*TRADE | Databento + E*TRADE |

**All backtest and sweep modes** run entirely from the local `.dbn.zst`
file — no E*TRADE credentials needed, no network calls after the file
is downloaded.

**Paper and live modes** use `ORBOptionsStrategy` and require both a
live Databento feed subscription and E*TRADE API keys. To trade shares
instead of options in live mode, swap `ORBOptionsStrategy` for
`ORBStrategy` in `run_live()` in `main.py`.

### Backtest output

Every run produces a SQLite database and two CSV files in `backtest/results/`:

```
<label>.db            — full event log, trades, daily summary (queryable)
<label>_trades.csv    — one row per completed round-trip trade
<label>_equity.csv    — daily cumulative P&L (equity curve)
```

Equity backtest summary includes: win rate, total P&L, avg win/loss,
profit factor, annualised Sharpe, max drawdown, long/short breakdown,
exit reason breakdown (stops/targets/EOD), and gap context breakdown.

Options backtest summary includes the above plus: total premium paid,
avg DTE, avg entry delta, and avg entry IV.

### Querying results

The SQLite database has four tables:

- **`runs`** — one row per backtest run recording every config parameter
  (rr_ratio, breakout_bars, slippage, etc.) and a UTC timestamp. Makes
  every `.db` file fully self-describing.
- **`events`** — every intraday state transition with a `detail` JSON field.
  Event types: RANGE_SET, RANGE_SKIPPED, GAP_SIGNAL, BREAKOUT_LONG/SHORT,
  RETEST_LONG/SHORT, WINDOW_EXPAND, ENTRY_LONG/SHORT, VOLUME_SIGNAL,
  STOP_LOSS, TAKE_PROFIT, EOD_CLOSE.
- **`trades`** — one row per completed round-trip, enriched with range context,
  gap signal, volume signal, stop/target prices, and retest metadata.
- **`daily_summary`** — one row per trading day with range, gap, and trade outcome.

Open the `.db` file in **DB Browser for SQLite** (sqlitebrowser.org) for
visual table browsing, or query with pandas:

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("backtest/results/orb_equity.db")

# What params produced these results?
pd.read_sql("SELECT * FROM runs", conn)

# --- Cross-run sweep analysis ---
# Summarise every .db file in results/ into a ranked comparison table
import glob

def summarise_run(path):
    c = sqlite3.connect(path)
    r = pd.read_sql("SELECT * FROM runs LIMIT 1", c)
    t = pd.read_sql("SELECT pnl FROM trades", c)
    if r.empty or t.empty:
        return None
    r = r.iloc[0]
    return {
        "label":         r["label"],
        "rr_ratio":      r["rr_ratio"],
        "breakout_bars": r["breakout_bars"],
        "retest_bars":   r["retest_bars"],
        "slippage":      r["slippage"],
        "trades":        len(t),
        "win_rate":      round((t.pnl > 0).mean(), 3),
        "total_pnl":     round(t.pnl.sum(), 2),
        "avg_pnl":       round(t.pnl.mean(), 2),
    }

sweep = pd.DataFrame(filter(None, [
    summarise_run(p) for p in glob.glob("backtest/results/*.db")
])).sort_values("total_pnl", ascending=False)
print(sweep.to_string(index=False))

# Replay one day — every event in order
events = pd.read_sql(
    "SELECT time, event_type, bar_close, detail "
    "FROM events WHERE date = '2024-03-14' ORDER BY id", conn)

# Win rate and avg P&L by gap direction
pd.read_sql(
    "SELECT gap_direction, COUNT(*) trades, "
    "ROUND(AVG(pnl),2) avg_pnl, "
    "ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) win_rate "
    "FROM trades GROUP BY gap_direction", conn)

# All trades on gap-up days
pd.read_sql(
    "SELECT date, direction, entry_price, exit_price, pnl, exit_reason "
    "FROM trades WHERE gap_direction = 'UP' ORDER BY date", conn)

# Days where the opening window expanded
pd.read_sql(
    "SELECT date, time, detail FROM events "
    "WHERE event_type = 'WINDOW_EXPAND' ORDER BY date", conn)

# Retests that preceded winning trades
pd.read_sql(
    "SELECT e.date, e.time, e.event_type, t.pnl "
    "FROM events e JOIN trades t ON e.date = t.date "
    "WHERE e.event_type IN ('RETEST_LONG','RETEST_SHORT') AND t.pnl > 0", conn)

# Days where range was skipped (too narrow)
pd.read_sql(
    "SELECT date, window_minutes, range_width "
    "FROM daily_summary WHERE range_skipped = 1 ORDER BY date", conn)

# P&L breakdown by exit reason
pd.read_sql(
    "SELECT exit_reason, COUNT(*) trades, "
    "ROUND(SUM(pnl),2) total_pnl, ROUND(AVG(pnl),2) avg_pnl "
    "FROM trades GROUP BY exit_reason", conn)
```

### Running with custom dates or parameters

The `python main.py backtest` command uses `config.py` defaults over the
full dataset. For custom date ranges, hand-picked days, or parameter
overrides, call `run_backtest()` directly from an ipython shell or script:

```python
from datetime import date
from backtest.run_orb_equity import run_backtest   # equity
# from backtest.run_orb_options import run_backtest  # options

# Hand-picked days — validate against trades you made manually
run_backtest(
    dates = [
        date(2024, 3, 14),
        date(2024, 3, 19),
        date(2024, 4, 2),
    ],
    label = "manual_validation",
)

# Date window
run_backtest(
    start_date = date(2024, 1, 2),
    end_date   = date(2024, 3, 31),
    label      = "Q1_2024",
)

# Override any strategy parameter inline
run_backtest(
    start_date    = date(2024, 1, 2),
    end_date      = date(2024, 12, 31),
    rr_ratio      = 3.0,
    breakout_bars = 5,
    retest_bars   = 3,
    slippage      = 0.02,
    label         = "2024_rr3_tight_entry",
)
```

Results always land in `backtest/results/<label>_trades.csv` and
`backtest/results/<label>_equity.csv` regardless of how you call it.

### Slippage model

A fixed per-share slippage (`SLIPPAGE = 0.01` in `config.py`) is applied
to every fill:

```
LONG  entry : fill = bar_close + slippage
SHORT entry : fill = bar_close - slippage
LONG  exit  : fill = exit_level - slippage
SHORT exit  : fill = exit_level + slippage
```

Adjust `SLIPPAGE` in `config.py` or pass `slippage=` directly to
`run_backtest()` to model different market conditions.

### Parameter sweep

Each sweep runs 8 preset parameter combinations and prints a ranked
comparison table. Edit the `configs` list in `run_orb_equity.py` (equity)
or `run_orb_options.py` (options) to customise the grid.

```
Label                          Trades  WinRate   TotalPnL  Sharpe     MaxDD     PF   L/S
ORB15m_RR2_b3r3                   312   54.8%   +8,420.50    1.24   1,840.00   1.72  198L/114S
ORB30m_RR3_b3r3                   187   57.2%   +6,110.00    1.08   2,200.00   1.65  ...
...
```

---

## Downloading historical data

```python
import databento as db

client = db.Historical(key="YOUR_KEY")

# Check cost first
cost = client.metadata.get_cost(
    dataset="XNAS.ITCH",
    schema="ohlcv-1s",
    symbols=["QQQ"],
    start="2018-05-01",
    end="2026-04-02",
)
print(f"Estimated cost: ${cost:.2f}")

# Download (saves to disk — no re-download needed)
client.timeseries.get_range(
    dataset="XNAS.ITCH",
    schema="ohlcv-1s",
    symbols=["QQQ"],
    start="2018-05-01",
    end="2026-04-02",
    path="/app/historical_data/QQQ_ohlcv_1s.dbn.zst",
)
```

For real options pricing, pull OPRA data (available from 2023-03-28):

```python
cost = client.metadata.get_cost(
    dataset="OPRA.PILLAR",
    schema="trades",
    symbols=["QQQ.OPT"],
    stype_in="parent",          # required for options chains
    start="2024-01-02",
    end="2024-03-31",
)
```

---

## E*TRADE authentication

E*TRADE uses OAuth 1.0a. On first run in `paper` or `live` mode, a
browser window opens asking you to authorise the app. Paste the
verifier code back into the terminal. This happens once per session.

On authentication, all accounts on the profile are printed:

```
  accountIdKey                   accountId       type         description
  AbCdEfGhIjKlMnOp               12345678        INDIVIDUAL   Brokerage  ← active
  XyZaBcDeFgHiJkLm               87654321        IRA          Roth IRA
```

Set `ETRADE_ACCOUNT_ID` in your `.env` to the `accountIdKey` of the
account you want to trade on. If unset, the first account is used.

---

## Key configuration (config.py)

### Credentials
| Setting | Description |
|---|---|
| `DATABENTO_API_KEY` | Databento API key |
| `ETRADE_CONSUMER_KEY` | E*TRADE OAuth consumer key |
| `ETRADE_CONSUMER_SECRET` | E*TRADE OAuth consumer secret |
| `ETRADE_ACCOUNT_ID` | accountIdKey to trade on (None = first account) |
| `SANDBOX_MODE` | True = sandbox, False = live trading |

### ORB signal
| Setting | Default | Description |
|---|---|---|
| `ORB_OPENING_RANGE_MINUTES` | `15` | Initial opening range window |
| `ORB_RR_RATIO` | `2.0` | Take-profit = RR × range width |
| `ORB_BREAKOUT_BARS` | `3` | Consecutive closes outside range to confirm breakout |
| `ORB_RETEST_BARS` | `3` | Consecutive closes inside window to credit retest |
| `ORB_RECONFIRM_BARS` | `3` | Consecutive closes outside window after retest to enter |
| `ORB_MIN_HOLD_MINUTES` | `30` | Min minutes between entry and EOD close |

### Adaptive range validation
| Setting | Default | Description |
|---|---|---|
| `ORB_MAX_WINDOW_MULTIPLIER` | `16` | Cap expansion at N × initial window (240 min for 15m) |
| `ORB_MIN_RANGE_PCT` | `0.5` | Range must be ≥ 50% of rolling average to be valid |
| `ORB_ROLLING_LOOKBACK_DAYS` | `50` | Rolling average lookback (days) |
| `ORB_MIN_BOOTSTRAP_DAYS` | `5` | Days before range validation kicks in |

### Gap detection
| Setting | Default | Description |
|---|---|---|
| `GAP_LOOKBACK_DAYS` | `50` | Rolling avg lookback for gap history |
| `GAP_NONE_THRESHOLD` | `0.001` | abs(gap) below this = direction NONE (0.1%) |

### Volume evaluation
| Setting | Default | Description |
|---|---|---|
| `VOL_LOOKBACK_DAYS` | `50` | Rolling avg lookback for volume history |
| `VOL_BARS_TO_TRACK` | `20` | Sliding window of recent bar volumes |

### Risk
| Setting | Default | Description |
|---|---|---|
| `MAX_DAILY_LOSS` | `1000.0` | Halt trading if day P&L drops below this |
| `MAX_RISK_PER_TRADE` | `500.0` | Max option premium per trade (options only) |

### Options-specific
| Setting | Default | Description |
|---|---|---|
| `ORB_TARGET_DTE` | `1` | Days to expiry (0=0DTE, 1=next day, 7=weekly) |
| `ORB_STRIKE_OFFSET_PCT` | `0.0` | 0.0=ATM, 0.005=0.5% OTM |

---

## Upgrading to real options pricing

The options strategy uses Black-Scholes by default. To upgrade to real
OPRA bid/ask data, implement the stub in `strategy/orb_options.py`:

```python
def _get_option_price(self, spot, option_type, bar_date):
    if self.use_real_pricing:
        # Look up OPRA record matching (symbol, strike, expiry, option_type)
        # at bar_date and return an OptionPrice with real bid/ask/greeks
        ...
```

Set `use_real_pricing = True` in the strategy constructor once implemented.

---

## Going live

1. Run `python main.py backtest` and `python main.py sweep_equity` —
   understand your strategy's historical characteristics before risking money
2. Run `python main.py paper` for at least a few days — confirm live
   signals match backtest behaviour
3. In `.env` set `ETRADE_ACCOUNT_ID` to your intended trading account
4. In `config.py` set `SANDBOX_MODE = False`
5. Run `python main.py live`
