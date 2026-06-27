import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent.parent / "news_bot.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_hash TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT NOT NULL,
                published TEXT,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                topic_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS group_articles (
                chat_id INTEGER NOT NULL,
                link_hash TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, link_hash)
            );
            CREATE TABLE IF NOT EXISTS translations (
                text_hash TEXT PRIMARY KEY,
                translated_text TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
    _migrate_subscriptions()
    _migrate_translations()


def _migrate_translations() -> None:
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE translations ADD COLUMN text_hash TEXT")
        except sqlite3.OperationalError:
            pass


def _migrate_subscriptions() -> None:
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN topic_id INTEGER")
        except sqlite3.OperationalError:
            pass


def link_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def is_article_sent(link: str) -> bool:
    h = link_hash(link)
    with get_conn() as conn:
        cur = conn.execute("SELECT 1 FROM articles WHERE link_hash = ?", (h,))
        return cur.fetchone() is not None


def mark_article_sent(title: str, url: str, source: str, published: str | None) -> None:
    h = link_hash(url)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO articles (link_hash, title, url, source, published, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (h, title, url, source, published, now),
        )
        conn.commit()


def get_subscribed_chats() -> list[int]:
    with get_conn() as conn:
        cur = conn.execute("SELECT chat_id FROM subscriptions")
        return [row["chat_id"] for row in cur.fetchall()]


def is_subscribed(chat_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("SELECT 1 FROM subscriptions WHERE chat_id = ?", (chat_id,))
        return cur.fetchone() is not None


def add_subscription(chat_id: int, added_by: int, topic_id: int | None = None) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO subscriptions (chat_id, added_by, added_at, topic_id) VALUES (?, ?, ?, ?)",
                (chat_id, added_by, now, topic_id),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_subscription(chat_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cur.rowcount > 0


def sync_config_groups(
    chat_ids: list[int], topics: dict[str, int] | None = None
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    with get_conn() as conn:
        for cid in chat_ids:
            topic_id = topics.get(str(cid)) if topics else None
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO subscriptions (chat_id, added_by, added_at, topic_id) VALUES (?, ?, ?, ?)",
                    (cid, 0, now, topic_id),
                )
                if cur.rowcount:
                    added += 1
                elif topic_id is not None:
                    conn.execute(
                        "UPDATE subscriptions SET topic_id = ? WHERE chat_id = ?",
                        (topic_id, cid),
                    )
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    return added


def is_group_article_sent(chat_id: int, link_hash: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM group_articles WHERE chat_id = ? AND link_hash = ?",
            (chat_id, link_hash),
        )
        return cur.fetchone() is not None


def mark_group_article_sent(chat_id: int, link_hash: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_articles (chat_id, link_hash, sent_at) VALUES (?, ?, ?)",
            (chat_id, link_hash, now),
        )
        conn.commit()


def get_subscriptions() -> list[tuple[int, int | None]]:
    with get_conn() as conn:
        cur = conn.execute("SELECT chat_id, topic_id FROM subscriptions")
        return [(row["chat_id"], row["topic_id"]) for row in cur.fetchall()]


def set_group_topic(chat_id: int, topic_id: int | None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET topic_id = ? WHERE chat_id = ?",
            (topic_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_group_topic(chat_id: int) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT topic_id FROM subscriptions WHERE chat_id = ?", (chat_id,)
        )
        row = cur.fetchone()
        return row["topic_id"] if row else None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def get_cached_translation(text: str, target_lang: str) -> str | None:
    h = text_hash(text)
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT translated_text FROM translations WHERE text_hash = ? AND target_lang = ?",
            (h, target_lang),
        )
        row = cur.fetchone()
        return row["translated_text"] if row else None


def cache_translation(text: str, translated: str, target_lang: str) -> None:
    h = text_hash(text)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO translations (text_hash, translated_text, target_lang, created_at) VALUES (?, ?, ?, ?)",
            (h, translated, target_lang, now),
        )
        conn.commit()


def filter_unsent_for_group(
    articles: list["Article"], chat_id: int, link_hashes: list[str] | None = None
) -> list["Article"]:
    if not articles:
        return []
    if link_hashes is None:
        link_hashes = [link_hash(a.url) for a in articles]
    seen = set()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT link_hash FROM group_articles WHERE chat_id = ? AND link_hash IN ({})".format(
                ",".join("?" for _ in link_hashes)
            ),
            [chat_id, *link_hashes],
        ).fetchall()
        seen = {r["link_hash"] for r in rows}
    return [a for i, a in enumerate(articles) if link_hashes[i] not in seen]
