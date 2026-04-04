"""
config.py — Central configuration. Edit this before running.
Keep secrets out of source control — use environment variables in production.
"""

import os


class Config:
    # --- Databento ---
    DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "YOUR_DATABENTO_KEY")
    DATASET  = "XNAS.ITCH"         # Nasdaq TotalView ITCH
    SYMBOLS  = ["QQQ"]             # Symbols to subscribe to
    SCHEMA   = "ohlcv-1s"          # 1-second bars for ORB strategy

    # --- E*TRADE ---
    ETRADE_CONSUMER_KEY    = os.getenv("ETRADE_CONSUMER_KEY", "YOUR_KEY")
    ETRADE_CONSUMER_SECRET = os.getenv("ETRADE_CONSUMER_SECRET", "YOUR_SECRET")
    SANDBOX_MODE = True            # Set False for live trading

    # --- Risk ---
    MAX_POSITION_SIZE = 10         # Shares per order
    MAX_DAILY_LOSS    = 500.0      # Stop trading if daily P&L drops below -$500

    # --- ORB Strategy ---
    ORB_OPENING_RANGE_MINUTES = 15     # How long to build the opening range
    ORB_RR_RATIO              = 2.0    # Take profit = 2x the range width

    # --- Historical data ---
    HISTORICAL_DATA_PATH = "/app/historical_data/QQQ_ohlcv_1s.dbn.zst"

    # --- Logging ---
    LOG_LEVEL = "INFO"
