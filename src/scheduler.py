import asyncio
import logging

from telegram.ext import Application

from src.bot import broadcast_news

logger = logging.getLogger(__name__)


async def poll_loop(app: Application, interval: int) -> None:
    """Check for new articles every `interval` minutes."""
    while True:
        try:
            await broadcast_news(app)
        except Exception as e:
            logger.exception("Error in poll loop: %s", e)
        await asyncio.sleep(interval * 60)
