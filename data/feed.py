"""
data/feed.py — Databento live streaming feed.
Yields parsed records to the caller via an async generator.
"""

import databento as db
from utils.logger import get_logger

logger = get_logger(__name__)


class DatabentoFeed:
    def __init__(self, api_key: str, dataset: str, symbols: list[str], schema: str):
        self.api_key = api_key
        self.dataset = dataset
        self.symbols = symbols
        self.schema = schema

    async def stream(self):
        """
        Async generator that yields records from the Databento live feed.
        Each record is a dataclass (e.g. MBP1Msg, TradeMsg) depending on schema.

        Usage:
            async for record in feed.stream():
                process(record)
        """
        client = db.Live(key=self.api_key)

        client.subscribe(
            dataset=self.dataset,
            schema=self.schema,
            symbols=self.symbols,
        )

        logger.info(
            f"Subscribed to {self.dataset} | schema={self.schema} | symbols={self.symbols}"
        )

        async for record in client:
            logger.debug(f"Tick received: {record}")
            yield record
