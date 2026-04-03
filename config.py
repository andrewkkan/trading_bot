"""
config.py — Central configuration. Edit this before running.
Keep secrets out of source control — use environment variables in production.
"""

import os


class Config:
    # --- Databento ---
    DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "YOUR_DATABENTO_KEY")
    DATASET = "XNAS.ITCH"          # Nasdaq TotalView ITCH
    SYMBOLS = ["AAPL", "TSLA"]     # Symbols to subscribe to
    SCHEMA = "mbp-1"               # Top-of-book quotes; alternatives: trades, ohlcv-1s

    # --- E*TRADE ---
    ETRADE_CONSUMER_KEY    = os.getenv("ETRADE_CONSUMER_KEY", "YOUR_KEY")
    ETRADE_CONSUMER_SECRET = os.getenv("ETRADE_CONSUMER_SECRET", "YOUR_SECRET")
    SANDBOX_MODE = True            # Set False for live trading

    # --- Risk ---
    MAX_POSITION_SIZE = 100        # Max shares per order
    MAX_DAILY_LOSS    = 500.0      # Stop trading if daily P&L drops below -$500

    # --- Logging ---
    LOG_LEVEL = "INFO"
