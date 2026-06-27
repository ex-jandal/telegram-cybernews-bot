import asyncio
import logging
import os
import sys
from pathlib import Path

import tomli
from dotenv import load_dotenv

from telegram.ext import ApplicationBuilder

from src.bot import register_handlers
from src.db import init_db, sync_config_groups
from src.scheduler import poll_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.toml")


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomli.load(f)


async def main() -> None:
    init_db()
    logger.info("Database initialized.")

    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)

    config = load_config()
    feeds = config["sources"]["feeds"]
    poll_interval = config["bot"]["poll_interval_minutes"]

    chat_ids = config.get("groups", {}).get("chat_ids", [])
    topics = config.get("groups", {}).get("topics", {})
    if chat_ids:
        added = sync_config_groups(chat_ids, topics)
        logger.info("Synced %d groups from config.", added)
    if topics:
        logger.info("Topics configured for %d groups.", len(topics))

    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN environment variable not set.")
        sys.exit(1)

    app = (
        ApplicationBuilder()
        .token(token)
        .build()
    )

    app.bot_data["feeds"] = feeds
    app.bot_data["poll_interval"] = poll_interval
    app.bot_data["message_delay"] = config["bot"]["message_delay_seconds"]
    app.bot_data["batch_size"] = config["bot"]["batch_size"]
    app.bot_data["batch_cooldown"] = config["bot"]["batch_cooldown_seconds"]
    app.bot_data["summary_max_chars"] = config["bot"]["summary_max_chars"]

    t = config.get("translation", {})
    app.bot_data["translation_enabled"] = t.get("enabled", False)
    app.bot_data["target_lang"] = t.get("target_lang", "ar")

    d = config.get("display", {})
    app.bot_data["thumbnail_mode"] = d.get("thumbnail_mode", "preview")

    register_handlers(app)

    async with app:
        logger.info("Starting bot...")
        await app.start()
        asyncio.create_task(poll_loop(app, poll_interval))
        logger.info("Bot is running. Polling every %d minutes.", poll_interval)
        await app.updater.start_polling()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
