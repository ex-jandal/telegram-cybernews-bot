import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import feedparser
import httpx
from deep_translator import GoogleTranslator

from src.db import is_article_sent, mark_article_sent

TRANSLATOR = GoogleTranslator(source="en", target="ar")

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def clean_summary(raw: str | None, max_chars: int = 300) -> str | None:
    if not raw:
        return None
    text = TAG_RE.sub("", raw)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rsplit(" ", 1)[0] + "…"


IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class Article:
    title: str
    url: str
    source: str
    published: str | None
    summary: str | None
    thumbnail_url: str | None = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _extract_link(entry: dict) -> str:
    raw = entry.get("link")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("href", "")
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("href", "") if isinstance(first, dict) else str(first)
    links = entry.get("links", [])
    if links:
        return links[0].get("href", "")
    return ""


def _extract_thumbnail(entry: dict) -> str | None:
    enclosures = entry.get("enclosures", [])
    for enc in enclosures:
        mime = (enc.get("type") or "").lower()
        href = enc.get("href") or enc.get("url", "")
        if href and ("image" in mime or mime.startswith("application/octet")):
            return href

    media_content = entry.get("media_content", [])
    for mc in media_content:
        url = mc.get("url", "")
        if url:
            return url

    media_thumbnail = entry.get("media_thumbnail", [])
    for mt in media_thumbnail:
        url = mt.get("url", "")
        if url:
            return url

    summary = entry.get("summary", "")
    if summary:
        match = IMG_RE.search(summary)
        if match:
            return match.group(1)

    content = entry.get("content", [])
    for c in content:
        if isinstance(c, dict):
            value = c.get("value", "")
            match = IMG_RE.search(value)
            if match:
                return match.group(1)

    return None


def fetch_feed(url: str) -> list[Article]:
    try:
        resp = httpx.get(
            url,
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return []

    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        logger.warning("Malformed feed %s: %s", url, feed.bozo_exception)

    articles: list[Article] = []

    for entry in feed.entries:
        title = entry.get("title", "Untitled")
        link = _extract_link(entry)
        published = entry.get("published", None)
        summary = entry.get("summary", None)

        if not link:
            continue

        thumbnail_url = _extract_thumbnail(entry)

        articles.append(
            Article(
                title=title,
                url=link,
                source=feed.feed.get("title", url),
                published=published,
                summary=summary,
                thumbnail_url=thumbnail_url,
            )
        )

    logger.info("Fetched %d articles from %s", len(articles), url)
    return articles


def get_new_articles(feeds: list[str]) -> list[Article]:
    new_articles: list[Article] = []
    seen_urls: set[str] = set()

    for feed_url in feeds:
        articles = fetch_feed(feed_url)
        for article in articles:
            if article.url in seen_urls:
                continue
            seen_urls.add(article.url)
            if not is_article_sent(article.url):
                new_articles.append(article)

    return new_articles


def mark_as_sent(articles: list[Article]) -> None:
    for a in articles:
        mark_article_sent(a.title, a.url, a.source, a.published)


def translate_article(article: Article, target_lang: str) -> Article:
    source_lang = "en"
    from src.db import cache_translation, get_cached_translation

    title = article.title
    summary = article.summary

    cached_title = get_cached_translation(title, target_lang)
    if cached_title:
        translated_title = cached_title
    else:
        try:
            tr = GoogleTranslator(source=source_lang, target=target_lang)
            translated_title = tr.translate(title)
            cache_translation(title, translated_title, target_lang)
        except Exception as e:
            logger.warning("Translation failed for title: %s", e)
            translated_title = title

    translated_summary = None
    if summary:
        cached_summary = get_cached_translation(summary, target_lang)
        if cached_summary:
            translated_summary = cached_summary
        else:
            try:
                tr = GoogleTranslator(source=source_lang, target=target_lang)
                translated_summary = tr.translate(summary)
                cache_translation(summary, translated_summary, target_lang)
            except Exception as e:
                logger.warning("Translation failed for summary: %s", e)
                translated_summary = summary

    return replace(article, title=translated_title, summary=translated_summary)
