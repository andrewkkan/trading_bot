"""
main.py — Entry point for the trading bot.

Run modes:
    python main.py backtest   — replay historical data, no orders sent
    python main.py live       — live feed + real E*TRADE order placement
    python main.py paper      — live feed, orders logged but not sent
"""

import sys
import asyncio
import databento as db

from config import Config
from strategy.orb import ORBStrategy
from broker.etrade import ETradeClient
from utils.logger import get_logger

logger = get_logger(__name__)


def run_backtest():
    """Replay the local .dbn.zst file tick by tick. No broker needed."""
    logger.info("=" * 60)
    logger.info("MODE: BACKTEST")
    logger.info(f"File: {Config.HISTORICAL_DATA_PATH}")
    logger.info(f"Opening range: {Config.ORB_OPENING_RANGE_MINUTES} min | RR: {Config.ORB_RR_RATIO}")
    logger.info("=" * 60)

    strategy = ORBStrategy(
        symbol=Config.SYMBOLS[0],
        quantity=Config.MAX_POSITION_SIZE,
        opening_range_minutes=Config.ORB_OPENING_RANGE_MINUTES,
        rr_ratio=Config.ORB_RR_RATIO,
        max_daily_loss=Config.MAX_DAILY_LOSS,
    )

    store = db.DBNStore.from_file(Config.HISTORICAL_DATA_PATH)

    total_bars   = 0
    total_orders = 0

    for record in store:
        total_bars += 1
        order = strategy.on_tick(record)
        if order:
            total_orders += 1
            logger.info(
                f"[BACKTEST ORDER] {order.side} {order.quantity} {order.symbol} "
                f"@ {order.order_type} | reason: {order.reason}"
            )

    logger.info("=" * 60)
    logger.info(f"Backtest complete. Bars processed: {total_bars:,} | Orders fired: {total_orders}")
    logger.info("=" * 60)


async def run_live(paper: bool = False):
    """Connect to Databento live feed and optionally send orders to E*TRADE."""
    mode_label = "PAPER" if paper else "LIVE"
    logger.info("=" * 60)
    logger.info(f"MODE: {mode_label}")
    logger.info("=" * 60)

    strategy = ORBStrategy(
        symbol=Config.SYMBOLS[0],
        quantity=Config.MAX_POSITION_SIZE,
        opening_range_minutes=Config.ORB_OPENING_RANGE_MINUTES,
        rr_ratio=Config.ORB_RR_RATIO,
        max_daily_loss=Config.MAX_DAILY_LOSS,
    )

    broker = None
    if not paper:
        broker = ETradeClient(
            consumer_key=Config.ETRADE_CONSUMER_KEY,
            consumer_secret=Config.ETRADE_CONSUMER_SECRET,
            sandbox=Config.SANDBOX_MODE,
        )
        broker.authenticate()

    client = db.Live(key=Config.DATABENTO_API_KEY)
    client.subscribe(
        dataset=Config.DATASET,
        schema=Config.SCHEMA,
        symbols=Config.SYMBOLS,
    )

    async for record in client:
        order = strategy.on_tick(record)
        if order:
            if paper:
                logger.info(
                    f"[PAPER] Would send: {order.side} {order.quantity} "
                    f"{order.symbol} | reason: {order.reason}"
                )
            else:
                result = broker.place_order(order)
                logger.info(f"Order placed: {result}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        run_backtest()
    elif mode == "paper":
        asyncio.run(run_live(paper=True))
    elif mode == "live":
        asyncio.run(run_live(paper=False))
    else:
        print("Usage: python main.py [backtest|paper|live]")
        sys.exit(1)
