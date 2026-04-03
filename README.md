# trading_bot

A real-time trading bot that streams market data via Databento and executes orders through E*TRADE.

## Structure

```
trading_bot/
├── main.py              # Entry point
├── config.py            # All settings and credentials
├── requirements.txt
├── data/
│   └── feed.py          # Databento live stream
├── strategy/
│   └── signals.py       # Signal logic + risk checks
├── broker/
│   └── etrade.py        # E*TRADE OAuth + order placement
└── utils/
    └── logger.py        # Shared logging
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your credentials as environment variables
export DATABENTO_API_KEY="your_key"
export ETRADE_CONSUMER_KEY="your_key"
export ETRADE_CONSUMER_SECRET="your_secret"

# 3. Run (sandbox mode is ON by default in config.py)
python main.py
```

## First run

On first run, E*TRADE will open a browser window asking you to authorize the app.
After logging in, paste the verifier code back into the terminal. This happens once per session.

## Customizing the strategy

Edit `strategy/signals.py` → `generate_signal()`. Return an `Order` to trade, or `None` to skip.

## Going live

In `config.py`, set `SANDBOX_MODE = False`. Make sure you have tested thoroughly in sandbox first.
