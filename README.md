# trading_bot

A real-time trading bot that streams market data via Databento and executes
orders through E*TRADE. Implements an Opening Range Breakout (ORB) strategy
with support for both equity (shares) and options execution.

## Project structure

```
trading_bot/
├── main.py                         # Entry point — all run modes
├── config.py                       # All settings and credentials
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── strategy/
│   ├── orb_base.py                 # Abstract base — shared ORB logic (edit here first)
│   ├── orb.py                      # Equity (shares) ORB — entry, stop, target
│   ├── orb_options.py              # Options ORB — sizing, Black-Scholes, OPRA hook
│   ├── option_pricing.py           # Black-Scholes pricer + greeks + IV estimates
│   └── utils.py                    # Shared time/date utilities
│
├── broker/
│   ├── etrade.py                   # E*TRADE OAuth + equity + option order placement
│   └── option_examples.py          # Runnable option order examples
│
├── backtest/
│   └── run_orb_options.py          # Backtest runner, stats, CSV export, param sweep
│
├── data/
│   └── feed.py                     # Databento live stream wrapper
│
└── utils/
    └── logger.py                   # Shared logging
```

## Strategy architecture

All ORB variants inherit from `ORBBase` in `strategy/orb_base.py`. The base
class owns everything shared: timestamp parsing, day reset, market hours gate,
daily loss limit, and opening range construction. Subclasses implement only
what is specific to their execution style.

```
ORBBase  (orb_base.py)
├── ORBStrategy        (orb.py)          — trades shares
└── ORBOptionsStrategy (orb_options.py)  — trades options
```

**If you want to change how the opening range is built** — different window
logic, volume filters, gap handling — edit `orb_base.py` and both strategies
pick it up automatically.

**If you want to change equity execution** — add short selling, change order
type — edit `orb.py` only.

**If you want to change options execution** — sizing, stop/target logic, or
plug in real OPRA pricing — edit `orb_options.py` only.

## Setup

```bash
# 1. Copy credentials template and fill in your keys
cp .env.example .env

# 2. Build and run with Docker (recommended)
docker compose up --build

# 3. Or run directly
pip install -r requirements.txt
python main.py backtest_options
```

## Run modes

```bash
python main.py backtest           # ORB equity backtest against local DBN file
python main.py backtest_options   # ORB options backtest (Black-Scholes pricing)
python main.py sweep              # Parameter sweep across 8 ORB configs
python main.py paper              # Live Databento feed, log orders but don't send
python main.py live               # Live feed + real E*TRADE order execution
```

Backtest and sweep modes require no credentials — they run entirely from the
local `.dbn.zst` file. Paper and live modes require both Databento and E*TRADE
keys.

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

# Download (saves to disk, no re-download needed)
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

## E*TRADE authentication

E*TRADE uses OAuth 1.0a. On first run in `paper` or `live` mode, a browser
window opens asking you to authorise the app. Paste the verifier code back
into the terminal. This happens once per session.

The `stdin_open: true` and `tty: true` settings in `docker-compose.yml` keep
stdin open so the verifier prompt works inside the container.

Backtest and sweep modes never touch E*TRADE — no authentication needed.

## Key configuration (config.py)

| Setting | Default | Description |
|---|---|---|
| `SANDBOX_MODE` | `True` | Set `False` for live trading |
| `ORB_OPENING_RANGE_MINUTES` | `15` | Length of opening range window |
| `ORB_TARGET_DTE` | `1` | Days to expiry for options (0=0DTE) |
| `ORB_STOP_LOSS_PCT` | `0.50` | Exit if option loses 50% of premium |
| `ORB_TARGET_MULT` | `2.0` | Exit if option gains 2× entry premium |
| `MAX_RISK_PER_TRADE` | `500.0` | Max premium dollars per trade |
| `MAX_DAILY_LOSS` | `1000.0` | Halt trading if day P&L drops below this |

## Upgrading to real options pricing

The options strategy uses Black-Scholes by default. To upgrade to real OPRA
bid/ask data, implement the stub in `strategy/orb_options.py`:

```python
def _get_option_price(self, spot, direction, bar_date):
    if self.use_real_pricing:
        # Load OPRA record matching (strike, expiry, direction, timestamp)
        # Return an OptionPrice built from real bid/ask/greeks
        ...
```

Set `use_real_pricing = True` in `config.py` once implemented. Everything
else — sizing, stops, targets, P&L tracking, CSV output — stays unchanged.

## Going live

1. Test thoroughly in backtest mode
2. Run in `paper` mode for at least a few days
3. In `config.py` set `SANDBOX_MODE = False`
4. Run `python main.py live`
