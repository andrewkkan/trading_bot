"""
main.py — Entry point for the trading bot.
Starts the Databento live feed and wires it to the strategy and broker.
"""

import asyncio
from config import Config
from data.feed import DatabentoFeed
from strategy.signals import SignalEngine
from broker.etrade import ETradeClient
from utils.logger import get_logger

logger = get_logger(__name__)


async def main():
    logger.info("Starting trading bot...")

    # Initialize components
    feed = DatabentoFeed(
        api_key=Config.DATABENTO_API_KEY,
        dataset=Config.DATASET,
        symbols=Config.SYMBOLS,
        schema=Config.SCHEMA,
    )
    signal_engine = SignalEngine(
        max_position_size=Config.MAX_POSITION_SIZE,
        max_daily_loss=Config.MAX_DAILY_LOSS,
    )
    broker = ETradeClient(
        consumer_key=Config.ETRADE_CONSUMER_KEY,
        consumer_secret=Config.ETRADE_CONSUMER_SECRET,
        sandbox=Config.SANDBOX_MODE,
    )

    # Authenticate with E*TRADE (requires browser step once per session)
    broker.authenticate()

    # Start the live feed — each tick is passed to on_tick()
    async for record in feed.stream():
        order = signal_engine.on_tick(record)
        if order:
            result = broker.place_order(order)
            logger.info(f"Order placed: {result}")


if __name__ == "__main__":
    asyncio.run(main())
