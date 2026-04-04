"""
config.py — Central configuration.
Keep secrets out of source control — use environment variables in production.
"""

import os


class Config:
    # --- Databento ---
    DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "YOUR_DATABENTO_KEY")
    DATASET  = "XNAS.ITCH"
    SYMBOLS  = ["QQQ"]
    SCHEMA   = "ohlcv-1s"

    # --- E*TRADE ---
    ETRADE_CONSUMER_KEY    = os.getenv("ETRADE_CONSUMER_KEY", "YOUR_KEY")
    ETRADE_CONSUMER_SECRET = os.getenv("ETRADE_CONSUMER_SECRET", "YOUR_SECRET")
    SANDBOX_MODE = True

    # --- Risk ---
    MAX_DAILY_LOSS     = 1000.0     # stop trading for the day beyond this loss
    MAX_RISK_PER_TRADE =  500.0     # max premium spent per option trade

    # --- ORB (equity) ---
    ORB_OPENING_RANGE_MINUTES = 15
    ORB_RR_RATIO              = 2.0

    # --- ORB Options ---
    ORB_TARGET_DTE        = 1       # 0=same day, 1=next day, 7=weekly
    ORB_STRIKE_OFFSET_PCT = 0.0     # 0.0=ATM, 0.005=0.5% OTM
    ORB_STRIKE_INTERVAL   = 1.0     # round strike to nearest $1
    ORB_STOP_LOSS_PCT     = 0.50    # exit if option loses 50% of value
    ORB_TARGET_MULT       = 2.0     # exit if option gains 2x entry price
    ORB_SPREAD_PCT        = 0.05    # bid/ask half-spread estimate (BS mode)

    # --- Historical data ---
    HISTORICAL_DATA_PATH = "/app/historical_data/QQQ_ohlcv_1s.dbn.zst"

    # --- Logging ---
    LOG_LEVEL = "INFO"
