import asyncio
import logging
from html import escape

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from src.db import (
    add_subscription,
    filter_unsent_for_group,
    get_group_topic,
    get_subscribed_chats,
    get_subscriptions,
    is_group_article_sent,
    is_subscribed,
    link_hash,
    mark_article_sent,
    mark_group_article_sent,
    remove_subscription,
    set_group_topic,
)
from src.fetcher import clean_summary, get_new_articles, translate_article

logger = logging.getLogger(__name__)

COMMANDS = """
• /news — جلب وإرسال آخر أخبار الأمن السيبراني
• /status — إظهار حالة البوت وعدد المشتركين
• /subscribe — (الأدمن/المالكون فقط) تسجيل هذا القروب للحصول على الاخبار تلقائيا
• /unsubscribe — (الأدمن/المالكون فقط) إلغاء الإشتراك
• /settopic <id> — (الأدمن/المالكون فقط) قم بتعيين فورم معين من الشات لأرسال الأخبار
• /sources — إظهار مصادر الأخبار التي تم تحديدها
• /help — إظهار رسالة المساعدة
"""

ALLOWED_CHAT_TYPES = ("group", "supergroup", "channel")
_news_lock = asyncio.Lock()
_in_flight: set[tuple[int, str]] = set()
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _article_text(article, summary_max_chars: int) -> str:
    title = escape(article.title)
    source = escape(article.source)
    url = escape(article.url)
    summary = clean_summary(article.summary, summary_max_chars)
    parts = [f"🔒 <b>{title}</b>"]
    if summary:
        parts.append(escape(summary))
    parts.append(f"📰 {source}  —  <a href=\"{url}\">Read more</a>")
    return "\n\n".join(parts)


async def _send_article_with_photo(
    bot, chat_id: int, article, text: str,
    message_thread_id: int | None = None,
) -> bool:
    try:
        img_data = await _download_image(article.thumbnail_url)
        kwargs = dict(
            chat_id=chat_id,
            photo=img_data,
            caption=text,
            parse_mode=ParseMode.HTML,
        )
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        await bot.send_photo(**kwargs)
        return True
    except Exception as e:
        logger.warning("Photo send failed for %s: %s", article.url, e)
        return False


async def _download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.content


async def _send_article_to_chat(
    bot, chat_id: int, article, delay: float, summary_max_chars: int,
    message_thread_id: int | None = None,
    target_lang: str | None = None,
    thumbnail_mode: str = "preview",
) -> bool:
    h = link_hash(article.url)
    key = (chat_id, h)
    if key in _in_flight:
        return True
    _in_flight.add(key)
    try:
        if is_group_article_sent(chat_id, h):
            return True

        if target_lang:
            article = translate_article(article, target_lang)
        text = _article_text(article, summary_max_chars)

        if thumbnail_mode == "rss" and article.thumbnail_url:
            ok = await _send_article_with_photo(
                bot, chat_id, article, text,
                message_thread_id=message_thread_id,
            )
            if not ok:
                await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                )
        else:
            kwargs = dict(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            await bot.send_message(**kwargs)

        mark_group_article_sent(chat_id, h)
        mark_article_sent(article.title, article.url, article.source, article.published)
        if delay > 0:
            await asyncio.sleep(delay)
        return True
    except Exception as e:
        logger.error("Failed to send article to %s: %s", chat_id, e)
        return False
    finally:
        _in_flight.discard(key)


def _get_config(context_or_app) -> tuple:
    feeds = context_or_app.bot_data.get("feeds", [])
    delay = context_or_app.bot_data.get("message_delay", 2.0)
    batch_sz = context_or_app.bot_data.get("batch_size", 15)
    cooldown = context_or_app.bot_data.get("batch_cooldown", 60.0)
    summary_max = context_or_app.bot_data.get("summary_max_chars", 300)
    translation_enabled = context_or_app.bot_data.get("translation_enabled", False)
    target_lang = context_or_app.bot_data.get("target_lang", "ar") if translation_enabled else None
    thumbnail_mode = context_or_app.bot_data.get("thumbnail_mode", "preview")
    return feeds, delay, batch_sz, cooldown, summary_max, target_lang, thumbnail_mode


async def _is_admin(chat, user_id: int) -> bool:
    try:
        admins = await chat.get_administrators()
        return user_id in {a.user.id for a in admins}
    except TelegramError as e:
        logger.warning("Failed to get admins for %s: %s", chat.id, e)
        return False


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"🔒 **روبوت أخبار الأمن السيبراني**\n\n"
        f"أحصل على آخر الأخبار من أهم مصادر الأمن السيبراني و "
        f"أرسلها إلى مجموعتك تلقائيًا.\n{COMMANDS}",
        parse_mode=ParseMode.MARKDOWN
    )


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(COMMANDS)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    feeds = context.bot_data.get("feeds", [])
    subs = get_subscribed_chats()
    trans_enabled = context.bot_data.get("translation_enabled", False)
    trans_lang = context.bot_data.get("target_lang", "ar")
    await update.message.reply_text(
        f"📡 <b>Bot Status</b>\n\n"
        f"News sources: {len(feeds)}\n"
        f"Subscribed chats: {len(subs)}\n"
        f"Poll interval: {context.bot_data.get('poll_interval', 20)} min\n"
        f"Delay between messages: {context.bot_data.get('message_delay', 2.0)}s\n"
        f"Batch size: {context.bot_data.get('batch_size', 15)}\n"
        f"Translation: {'✅ ' + trans_lang.upper() if trans_enabled else '❌ Off'}",
        parse_mode=ParseMode.HTML,
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    feeds, delay, batch_sz, _cooldown, summary_max, target_lang, thumbnail_mode = _get_config(context)

    if not feeds:
        await update.message.reply_text("No news sources configured.")
        return

    sent = await update.message.reply_text("Fetching latest cybersecurity news...")

    async with _news_lock:
        all_articles = get_new_articles(feeds)
    if not all_articles:
        await sent.edit_text("No new articles found.")
        return

    unsent = filter_unsent_for_group(all_articles, chat_id)
    if not unsent:
        await sent.edit_text("No new articles for this chat.")
        return

    target = unsent[:batch_sz]
    await sent.edit_text(f"Sending {len(target)} articles...")

    delivered = 0
    for article in target:
        ok = await _send_article_to_chat(
            context.bot, chat_id, article, delay, summary_max,
            message_thread_id=thread_id, target_lang=target_lang,
            thumbnail_mode=thumbnail_mode,
        )
        if ok:
            delivered += 1

    if delivered == 0:
        await sent.edit_text("Failed to send articles. Try again later.")
    else:
        try:
            await sent.delete()
        except Exception:
            pass


async def subscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ALLOWED_CHAT_TYPES:
        await update.message.reply_text("This command only works in groups or channels.")
        return

    if not await _is_admin(chat, user.id):
        await update.message.reply_text("Only admins and the owner can use /subscribe.")
        return

    if is_subscribed(chat.id):
        await update.message.reply_text("This chat is already subscribed.")
        return

    add_subscription(chat.id, user.id)
    await update.message.reply_text(
        "✅ This chat is now subscribed to cybersecurity news. New articles will be sent automatically."
    )


async def unsubscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ALLOWED_CHAT_TYPES:
        await update.message.reply_text("This command only works in groups or channels.")
        return

    if not await _is_admin(chat, user.id):
        await update.message.reply_text("Only admins and the owner can use /unsubscribe.")
        return

    if not is_subscribed(chat.id):
        await update.message.reply_text("This chat is not subscribed.")
        return

    remove_subscription(chat.id)
    await update.message.reply_text("❌ This chat has been unsubscribed from news updates.")


async def settopic(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ALLOWED_CHAT_TYPES:
        await update.message.reply_text("This command only works in groups or channels.")
        return

    if not await _is_admin(chat, user.id):
        await update.message.reply_text("Only admins and the owner can use /settopic.")
        return

    if not is_subscribed(chat.id):
        await update.message.reply_text("Subscribe first with /subscribe.")
        return

    args = _context.args if _context else None
    if not args:
        current = get_group_topic(chat.id)
        if current is not None:
            await update.message.reply_text(
                f"Current topic ID: <code>{current}</code>.\n"
                f"Usage: <code>/settopic {'clear' if current is not None else '<id>'}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "Usage: <code>/settopic {'clear' if current is not None else '<id>'}</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    if args[0].lower() in ("clear", "none", "off", "0"):
        set_group_topic(chat.id, None)
        await update.message.reply_text("🧹 Topic cleared. News will go to General.")
        return

    try:
        topic_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid topic ID. Provide a number.")
        return

    set_group_topic(chat.id, topic_id)
    await update.message.reply_text(
        f"✅ News will now be sent to topic <code>{topic_id}</code>.",
        parse_mode=ParseMode.HTML,
    )


async def sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    feeds = context.bot_data.get("feeds", [])
    if not feeds:
        await update.message.reply_text("No news sources configured.")
        return

    lines = [f"{i+1}. {url}" for i, url in enumerate(feeds)]
    await update.message.reply_text(
        "📡 <b>Configured news sources:</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def _send_batch(
    bot, chat_id: int, articles: list, delay: float,
    summary_max: int, target_lang: str | None, topic_id: int | None,
    thumbnail_mode: str = "preview",
) -> int:
    delivered = 0
    for article in articles:
        ok = await _send_article_to_chat(
            bot, chat_id, article, delay, summary_max,
            message_thread_id=topic_id, target_lang=target_lang,
            thumbnail_mode=thumbnail_mode,
        )
        if ok:
            delivered += 1
    return delivered


async def _broadcast_to_group(
    bot, chat_id: int, topic_id: int | None, all_articles: list,
    delay: float, batch_sz: int, cooldown: float, summary_max: int,
    target_lang: str | None, thumbnail_mode: str,
) -> None:
    unsent = filter_unsent_for_group(all_articles, chat_id)
    if not unsent:
        return

    total = len(unsent)
    sent = 0
    offset = 0

    while offset < total:
        batch = unsent[offset : offset + batch_sz]
        delivered = await _send_batch(
            bot, chat_id, batch, delay, summary_max,
            target_lang, topic_id, thumbnail_mode,
        )
        sent += delivered
        offset += batch_sz

        logger.info(
            "Sent %d/%d articles to chat %s (batch %d/%d).",
            sent, total, chat_id, offset // batch_sz,
            (total + batch_sz - 1) // batch_sz,
        )

        if offset < total and cooldown > 0:
            logger.info(
                "Waiting %ds before next batch for chat %s.",
                cooldown, chat_id,
            )
            await asyncio.sleep(cooldown)


async def broadcast_news(app: Application) -> None:
    feeds, delay, batch_sz, cooldown, summary_max, target_lang, thumbnail_mode = _get_config(app)

    if not feeds:
        logger.warning("No feeds configured, skipping broadcast.")
        return

    async with _news_lock:
        all_articles = get_new_articles(feeds)
    if not all_articles:
        logger.info("No new articles to broadcast.")
        return

    subscriptions = get_subscriptions()
    if not subscriptions:
        logger.info("No subscribed chats to broadcast to.")
        return

    tasks = [
        _broadcast_to_group(
            app.bot, chat_id, topic_id, all_articles,
            delay, batch_sz, cooldown, summary_max, target_lang, thumbnail_mode,
        )
        for chat_id, topic_id in subscriptions
    ]
    await asyncio.gather(*tasks)


def register_handlers(dp: Application) -> None:
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("news", news))
    dp.add_handler(CommandHandler("latest", news))
    dp.add_handler(CommandHandler("subscribe", subscribe))
    dp.add_handler(CommandHandler("unsubscribe", unsubscribe))
    dp.add_handler(CommandHandler("settopic", settopic))
    dp.add_handler(CommandHandler("sources", sources))
