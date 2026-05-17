"""
Gaming News Telegram Channel Bot (v2.0.0)
-----------------------------------------
Фокус: PlayStation, PS Plus, Xbox / Game Pass — ігри місяця, підписки, релізи.
Пости: шаблон UA + RU (Gemini). Кілька разів на день, без спаму.
Роздачі: лише PS/Xbox і лише коли немає свіжих новин.

Запуск:  python gaming_bot.py
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

# ──────────────────────── Logging ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gaming_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("gaming_bot")

# ──────────────────────── Config ──────────────────────────────────────────────
load_dotenv()

TOKEN           = os.getenv("GAMING_BOT_TOKEN", "")
CHANNEL_ID      = os.getenv("GAMING_CHANNEL_ID", "@your_gaming_channel")
RAWG_KEY        = os.getenv("RAWG_API_KEY", "")          # https://rawg.io/apidocs
NEWS_API_KEY    = os.getenv("NEWS_API_KEY", "")           # https://newsapi.org/  (optional)
GEMINI_KEY      = os.getenv("GEMINI_KEY", "")             # https://aistudio.google.com/
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))

GEMINI_MODEL    = "gemini-2.5-flash"
BOT_VERSION     = "2.0.0"

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default

DB_PATH         = "gaming_bot.db"
CHECK_INTERVAL  = _env_int("CHECK_INTERVAL_MIN", 60) * 60       # перевірка раз на годину
NEWS_MIN_POST_GAP = _env_int("NEWS_MIN_POST_GAP_HOURS", 4) * 60 * 60  # ~4–6 новин/день
MIN_POST_GAP    = NEWS_MIN_POST_GAP
MAX_POSTS_PER_CYCLE = 1
MAX_POSTS_PER_DAY   = _env_int("MAX_POSTS_PER_DAY", 5)
MAX_GIVEAWAYS_PER_DAY  = _env_int("MAX_GIVEAWAYS_PER_DAY", 1)
MAX_GIVEAWAYS_PER_WEEK = _env_int("MAX_GIVEAWAYS_PER_WEEK", 2)
MIN_HOURS_BETWEEN_GIVEAWAYS = _env_int("MIN_HOURS_BETWEEN_GIVEAWAYS", 48)
GIVEAWAY_IF_NO_NEWS_HOURS = _env_int("GIVEAWAY_IF_NO_NEWS_HOURS", 24)  # роздача лише якщо N год без новин
FALLBACK_HOURS  = _env_int("FALLBACK_HOURS", 72)
PHOTO_CAPTION_MAX = 1024
TEXT_MESSAGE_MAX  = 4000

# Роздачі: пріоритет PlayStation / Xbox (без PC-спаму)
GIVEAWAY_STORE_PRIORITY = (
    ("ps",     "playstation.com"),
    ("xbox",   "xbox.com"),
)
GIVEAWAY_STORE_SKIP = (
    "itch.io", "indiegala.com", "onstove.com", "gamerpower.com",
    "steampowered.com", "epicgames.com", "gog.com",
)

# ──────────────────────── RSS: лише PlayStation / Xbox ─────────────────────────
RSS_SOURCES = [
    {
        "name": "Push Square",
        "url": "https://www.pushsquare.com/feeds/news",
        "category": "news",
        "image_fallback": "https://www.pushsquare.com/images/icons/ps_icon.png",
    },
    {
        "name": "PlayStation Blog",
        "url": "https://blog.playstation.com/feed/",
        "category": "news",
        "image_fallback": "https://blog.playstation.com/favicon.ico",
    },
    {
        "name": "Pure Xbox",
        "url": "https://www.purexbox.com/feeds/news",
        "category": "news",
        "image_fallback": "https://www.purexbox.com/images/icons/xbox_icon.png",
    },
    {
        "name": "Eurogamer",
        "url": "https://www.eurogamer.net/feed",
        "category": "news",
        "image_fallback": "https://www.eurogamer.net/images/2023/08/eurogamer_icon.png",
    },
    {
        "name": "VG247",
        "url": "https://www.vg247.com/feed",
        "category": "news",
        "image_fallback": "https://www.vg247.com/wp-content/uploads/2023/01/vg247-logo.svg",
    },
]

# Ключові слова: PS, PS Plus, Xbox Game Pass, ігри місяця
PLATFORM_KEYWORDS = (
    "playstation", "ps5", "ps4", "ps plus", "ps+", "playstation plus",
    "ps plus essential", "ps plus extra", "ps plus premium",
    "xbox", "xbox series", "game pass", "xbox game pass", "pc game pass",
    "games with gold", "core games",
)
MONTHLY_LINEUP_KEYWORDS = (
    "monthly games", "this month", "next month", "coming to ps",
    "coming to xbox", "leaving ", "lineup", "day one", "day 1",
    "games arriving", "free games for", "subscription",
)
JUNK_KEYWORDS = (
    "oled tv", "gaming monitor", "graphics card", "best tv",
    "black friday tv", "cyber monday tv", "how to watch",
    "nintendo switch", "zelda only", "pokemon only", "pc only",
    "steam deck only", "best gaming laptop", "best gaming phone",
)

_PS_XBOX_RSS_SOURCES = frozenset({"Push Square", "PlayStation Blog", "Pure Xbox"})
RAWG_PS_XBOX = ("playstation", "xbox")

# ──────────────────────── Giveaway APIs (лише PS / Xbox) ─────────────────────
GAMERPOWER_PS   = "https://www.gamerpower.com/api/giveaways?platform=ps4&type=game&sort-by=date"
GAMERPOWER_XBOX = "https://www.gamerpower.com/api/giveaways?platform=xbox-one&type=game&sort-by=date"

CONTENT_CATEGORIES = ("news", "update", "trailer", "release")

# Category emojis
CATEGORY_EMOJI = {
    "news":      "🎮",
    "giveaway":  "🎁",
    "release":   "🚀",
    "update":    "🔧",
    "sale":      "💸",
    "review":    "⭐",
    "trailer":   "🎬",
    "esports":   "🏆",
}

PLATFORM_EMOJI = {
    "pc":         "🖥️",
    "steam":      "🎮",
    "epic":       "🟣",
    "gog":        "🟡",
    "ps4":        "🎮",
    "ps5":        "🎮",
    "playstation": "🎮",
    "xbox":       "🟩",
    "nintendo":   "🔴",
    "mobile":     "📱",
    "android":    "🤖",
    "ios":        "🍎",
}

GAMING_KEYWORDS = [
    "game", "gaming", "steam", "epic", "playstation", "xbox", "nintendo",
    "update", "patch", "dlc", "release", "launch", "giveaway", "free",
    "esports", "trailer", "review", "fps", "rpg", "mmorpg", "shooter",
    "indie", "aaa", "console", "pc", "multiplayer", "open world", "sequel",
]

# ──────────────────────── Database ────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hash        TEXT    UNIQUE NOT NULL,
            title       TEXT,
            url         TEXT,
            category    TEXT,
            source      TEXT,
            posted_at   REAL    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn

def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_state(conn: sqlite3.Connection, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?,?)", (key, value))
    conn.commit()

def is_posted(conn: sqlite3.Connection, h: str) -> bool:
    return conn.execute("SELECT 1 FROM posted WHERE hash=?", (h,)).fetchone() is not None

def mark_posted(conn: sqlite3.Connection, h: str, title: str, url: str, category: str, source: str):
    conn.execute(
        "INSERT OR IGNORE INTO posted(hash,title,url,category,source,posted_at) VALUES(?,?,?,?,?,?)",
        (h, title[:500], url[:1000], category, source, time.time()),
    )
    conn.commit()

def hours_since_last_post(conn: sqlite3.Connection) -> float:
    last = conn.execute("SELECT MAX(posted_at) FROM posted").fetchone()[0]
    if not last:
        return 9999.0
    return (time.time() - last) / 3600

def count_posts_since(conn: sqlite3.Connection, hours: float, category: Optional[str] = None) -> int:
    since = time.time() - hours * 3600
    if category:
        row = conn.execute(
            "SELECT COUNT(*) FROM posted WHERE posted_at > ? AND category = ?",
            (since, category),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM posted WHERE posted_at > ?", (since,)
        ).fetchone()
    return int(row[0]) if row else 0

def hours_since_last_category(conn: sqlite3.Connection, category: str) -> float:
    last = conn.execute(
        "SELECT MAX(posted_at) FROM posted WHERE category = ?", (category,)
    ).fetchone()[0]
    if not last:
        return 9999.0
    return (time.time() - last) / 3600

def hours_since_last_content_post(conn: sqlite3.Connection) -> float:
    """Годин з останнього поста про новини/реліз (не роздачу)."""
    last = conn.execute(
        "SELECT MAX(posted_at) FROM posted WHERE category IN ('news','update','trailer','release')"
    ).fetchone()[0]
    if not last:
        return 9999.0
    return (time.time() - last) / 3600

def article_text_blob(article: dict) -> str:
    return f"{article.get('title', '')} {article.get('summary', '')} {article.get('source', '')}".lower()

def is_junk_article(article: dict) -> bool:
    blob = article_text_blob(article)
    return any(k in blob for k in JUNK_KEYWORDS)

def is_platform_focus_article(article: dict) -> bool:
    """PS, PS Plus, Xbox Game Pass, ігри місяця, консольні релізи."""
    if is_junk_article(article):
        return False
    blob = article_text_blob(article)
    if any(k in blob for k in PLATFORM_KEYWORDS):
        return True
    if any(k in blob for k in MONTHLY_LINEUP_KEYWORDS):
        return True
    return False

def passes_content_filter(article: dict) -> bool:
    """Джерела PS/Xbox — ширше; Eurogamer/VG247 — лише за ключовими словами."""
    if is_junk_article(article):
        return False
    if article.get("source") in _PS_XBOX_RSS_SOURCES:
        return True
    return is_platform_focus_article(article)

def platform_priority_score(article: dict) -> float:
    blob = article_text_blob(article)
    score = freshness_boost(article)
    if any(k in blob for k in MONTHLY_LINEUP_KEYWORDS):
        score += 6.0
    if any(k in blob for k in ("ps plus", "ps+", "playstation plus", "game pass", "games with gold")):
        score += 5.0
    if any(k in blob for k in ("ps5", "ps4", "xbox series", "xbox one")):
        score += 2.0
    return score

def sort_platform_content(articles: list[dict]) -> list[dict]:
    def key(a):
        pd = a.get("pub_date")
        ts = pd.timestamp() if pd else 0
        return (platform_priority_score(a), ts)
    return sorted(articles, key=key, reverse=True)

def is_ps_xbox_giveaway(article: dict) -> bool:
    if not is_quality_giveaway(article):
        return False
    return giveaway_store_key(article.get("url", "")) in ("ps", "xbox")

def giveaway_store_key(url: str) -> str:
    url = (url or "").lower()
    for key, domain in GIVEAWAY_STORE_PRIORITY:
        if domain in url:
            return key
    return "other"

def recent_giveaway_stores(conn: sqlite3.Connection, days: int = 7) -> set[str]:
    since = time.time() - days * 86400
    rows = conn.execute(
        "SELECT url FROM posted WHERE category = 'giveaway' AND posted_at > ?",
        (since,),
    ).fetchall()
    return {giveaway_store_key(r[0]) for r in rows if r[0]}

def is_quality_giveaway(article: dict) -> bool:
    """Skip obscure indie-aggregator spam; keep major store deals."""
    url = (article.get("url") or "").lower()
    if any(skip in url for skip in GIVEAWAY_STORE_SKIP):
        return False
    if any(domain in url for _, domain in GIVEAWAY_STORE_PRIORITY):
        return True
    return giveaway_store_key(url) == "other" and "free" in (article.get("title") or "").lower()

def pick_diverse_giveaway(giveaways: list[dict], conn: sqlite3.Connection) -> Optional[dict]:
    """Pick one giveaway, preferring a store not used in the last week."""
    recent = recent_giveaway_stores(conn)
    by_store: dict[str, list[dict]] = {}
    for g in giveaways:
        if not is_quality_giveaway(g):
            continue
        by_store.setdefault(giveaway_store_key(g.get("url", "")), []).append(g)

    for key, _domain in GIVEAWAY_STORE_PRIORITY:
        if key in by_store and key not in recent:
            return by_store[key][0]

    for key, _domain in GIVEAWAY_STORE_PRIORITY:
        if key in by_store:
            return by_store[key][0]

    return None

def required_gap_seconds(category: str) -> int:
    if category == "giveaway":
        return MIN_HOURS_BETWEEN_GIVEAWAYS * 3600
    return NEWS_MIN_POST_GAP

def classify_rss_category(title: str, summary: str = "") -> str:
    """Визначити тип матеріалу за заголовком (новина / патч / трейлер / реліз)."""
    t = f"{title} {summary}".lower()
    if any(w in t for w in ("trailer", "gameplay trailer", "cinematic", "трейлер")):
        return "trailer"
    if any(w in t for w in ("patch", "hotfix", "dlc", "update ", "updated", "оновлення", "патч")):
        return "update"
    if any(w in t for w in ("release date", "releases on", "launching", "coming to", "реліз", "вихід")):
        return "release"
    return "news"

def freshness_boost(article: dict) -> float:
    pd = article.get("pub_date")
    if not pd:
        return 0.0
    age_h = (datetime.now(timezone.utc) - pd).total_seconds() / 3600
    if age_h <= 3:
        return 3.0
    if age_h <= 12:
        return 1.5
    return 0.0

def sort_content(articles: list[dict]) -> list[dict]:
    def key(a):
        pd = a.get("pub_date")
        ts = pd.timestamp() if pd else 0
        return (freshness_boost(a), ts)
    return sorted(articles, key=key, reverse=True)

def can_post_giveaway(conn: sqlite3.Connection) -> bool:
    if count_posts_since(conn, 24, "giveaway") >= MAX_GIVEAWAYS_PER_DAY:
        return False
    if count_posts_since(conn, 168, "giveaway") >= MAX_GIVEAWAYS_PER_WEEK:
        return False
    if hours_since_last_category(conn, "giveaway") < MIN_HOURS_BETWEEN_GIVEAWAYS:
        return False
    return True

def select_articles_for_cycle(articles: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """
    Спочатку новини PS / PS+ / Xbox. Роздача — лише якщо давно не було контент-постів.
    """
    if count_posts_since(conn, 24) >= MAX_POSTS_PER_DAY:
        log.info("Daily post cap reached (%d)", MAX_POSTS_PER_DAY)
        return []

    content = [
        a for a in articles
        if a.get("category") in CONTENT_CATEGORIES and passes_content_filter(a)
    ]
    content = sort_platform_content(content)

    giveaways = [
        a for a in articles
        if a.get("category") == "giveaway" and is_ps_xbox_giveaway(a)
    ]

    if content:
        pick = content[0]
        log.info(
            "Selected %s (score %.1f): %s",
            pick.get("category"), platform_priority_score(pick), pick.get("title", "")[:55],
        )
        return [pick]

    hours_no_content = hours_since_last_content_post(conn)
    if (
        hours_no_content >= GIVEAWAY_IF_NO_NEWS_HOURS
        and can_post_giveaway(conn)
        and giveaways
    ):
        pick = pick_diverse_giveaway(giveaways, conn)
        if pick:
            log.info(
                "Giveaway (no PS/Xbox news %.0fh): %s",
                hours_no_content, pick.get("title", "")[:55],
            )
            return [pick]

    if hours_no_content >= GIVEAWAY_IF_NO_NEWS_HOURS and giveaways:
        log.info("Giveaway skipped (rate limit), queue has %d", len(giveaways))
    return []

# ──────────────────────── Helpers ─────────────────────────────────────────────

def make_hash(*parts: str) -> str:
    text = "|".join(str(p) for p in parts)
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def truncate(text: str, limit: int = PHOTO_CAPTION_MAX) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."

def clean_html(text: str) -> str:
    """Strip HTML tags from text."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def is_gaming_related(title: str, description: str = "") -> bool:
    """Check if an article is gaming-related."""
    combined = (title + " " + description).lower()
    return any(kw in combined for kw in GAMING_KEYWORDS)

_INVALID_SLUGS = frozenset({"[]", "{}", "null", "none", "undefined"})

def normalize_article_url(url: str) -> str:
    """Return a clean http(s) URL or empty string."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    return url

def normalize_store_url(url: str) -> str:
    """Fix known-bad store URLs (Epic slugs, locale, GamerPower leftovers)."""
    url = normalize_article_url(url)
    if not url:
        return ""
    if "gamerpower.com" in url:
        return ""  # force re-resolve; never post intermediary pages
    if "/p/[]" in url or url.rstrip("/").endswith("/p"):
        return "https://store.epicgames.com/uk/free-games"
    # Epic without /uk/ → Ukrainian store (GamerPower redirects often omit locale)
    m = re.match(r"(https://store\.epicgames\.com)/p/([\w\-]+)/?$", url, re.I)
    if m:
        return f"{m.group(1)}/uk/p/{m.group(2)}"
    return url

def strip_urls_from_text(text: str) -> str:
    """Remove URLs from AI/RSS text so users don't click wrong links in the body."""
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def pick_epic_slug(product_slug: object, url_slug: object) -> str:
    """Epic API sometimes returns productSlug as the literal string '[]'."""
    for raw in (product_slug, url_slug):
        if raw is None:
            continue
        slug = str(raw).strip().lower()
        if not slug or slug in _INVALID_SLUGS:
            continue
        if re.fullmatch(r"[\w\-]+", slug):
            return slug
    return ""

def epic_store_url(el: dict) -> str:
    title = (el.get("title") or "").lower()
    # Mystery-game slugs from Epic API often point to invalid promo pages
    if "mystery" in title:
        return "https://store.epicgames.com/uk/free-games"
    slug = pick_epic_slug(el.get("productSlug"), el.get("urlSlug"))
    if slug:
        return f"https://store.epicgames.com/uk/p/{slug}"
    return "https://store.epicgames.com/uk/free-games"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

async def resolve_final_url(client: httpx.AsyncClient, url: str) -> str:
    """
    Follow redirects so GamerPower /open/... links become direct Steam/Epic/etc. URLs.
    """
    url = normalize_article_url(url)
    if not url:
        return ""
    if "gamerpower.com" not in url:
        return normalize_store_url(url)

    try:
        resp = await client.get(
            url, follow_redirects=True, timeout=20, headers=_BROWSER_HEADERS
        )
        final = normalize_store_url(str(resp.url))
        if final and "gamerpower.com" not in final:
            return final
        # Some pages use HTML/JS redirect — parse store link from body
        if resp.status_code == 200 and resp.text:
            for pattern in (
                r'https://store\.steampowered\.com/app/\d+[^\s"\'<>]*',
                r'https://store\.epicgames\.com(?:/uk)?/p/[\w\-]+',
                r'https://[\w\-]+\.itch\.io/[\w\-]+',
            ):
                m = re.search(pattern, resp.text)
                if m:
                    return normalize_store_url(m.group(0))
    except Exception as exc:
        log.warning("Redirect resolve failed for %s: %s", url, exc)
    return ""

def extract_image_from_rss_entry(entry_xml: ET.Element, ns: dict) -> Optional[str]:
    """Try to extract an image URL from an RSS entry XML element."""
    # Check media:content
    for tag in ["media:content", "{http://search.yahoo.com/mrss/}content"]:
        el = entry_xml.find(tag, ns)
        if el is not None:
            url = el.get("url", "")
            if url and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                return url

    # Check media:thumbnail
    for tag in ["media:thumbnail", "{http://search.yahoo.com/mrss/}thumbnail"]:
        el = entry_xml.find(tag, ns)
        if el is not None:
            url = el.get("url", "")
            if url:
                return url

    # Check enclosure
    enc = entry_xml.find("enclosure", ns)
    if enc is not None:
        url = enc.get("url", "")
        ctype = enc.get("type", "")
        if "image" in ctype or any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png"]):
            return url

    # Search for image inside description/content
    for tag in ["content:encoded", "{http://purl.org/rss/1.0/modules/content/}encoded", "description"]:
        el = entry_xml.find(tag, ns)
        if el is not None and el.text:
            match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', el.text)
            if match:
                url = match.group(1)
                if url.startswith("http"):
                    return url

    return None

async def fetch_url(client: httpx.AsyncClient, url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None

async def verify_image_url(client: httpx.AsyncClient, url: str) -> bool:
    """Check that image URL is reachable and is an actual image."""
    if not url or not url.startswith("http"):
        return False
    try:
        resp = await client.head(url, timeout=10, follow_redirects=True)
        ct = resp.headers.get("content-type", "")
        return resp.status_code == 200 and ("image" in ct or url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")))
    except Exception:
        return False

# ──────────────────────── RSS Fetcher ─────────────────────────────────────────

def parse_rss_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return None

async def fetch_rss(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """Fetch and parse RSS feed. Returns list of article dicts."""
    data = await fetch_url(client, source["url"])
    if not data:
        return []
    articles = []
    ns = {
        "media":   "http://search.yahoo.com/mrss/",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom":    "http://www.w3.org/2005/Atom",
        "dc":      "http://purl.org/dc/elements/1.1/",
    }
    try:
        root = ET.fromstring(data)
        # Detect Atom vs RSS
        tag = root.tag.lower()
        is_atom = "atom" in tag or root.tag == "{http://www.w3.org/2005/Atom}feed"

        if is_atom:
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")
            for entry in entries:
                def atom_text(t):
                    el = entry.find(f"{{http://www.w3.org/2005/Atom}}{t}")
                    return el.text.strip() if el is not None and el.text else ""

                title = atom_text("title")
                link_el = entry.find("{http://www.w3.org/2005/Atom}link[@rel='alternate']")
                if link_el is None:
                    link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                url = normalize_article_url(link_el.get("href", "") if link_el is not None else "")
                summary = clean_html(atom_text("summary") or atom_text("content"))
                pub_date = parse_rss_date(atom_text("published") or atom_text("updated"))
                img = extract_image_from_rss_entry(entry, ns)
                articles.append({
                    "title": title, "url": url, "summary": summary,
                    "pub_date": pub_date, "image": img,
                    "source": source["name"], "category": source["category"],
                    "image_fallback": source.get("image_fallback"),
                })
        else:
            channel = root.find("channel")
            if channel is None:
                channel = root
            for item in channel.findall("item"):
                def rss_text(t):
                    el = item.find(t)
                    return el.text.strip() if el is not None and el.text else ""

                title   = rss_text("title")
                url     = normalize_article_url(rss_text("link"))
                if not url:
                    guid = rss_text("guid")
                    if guid.startswith("http"):
                        url = normalize_article_url(guid)
                summary = clean_html(rss_text("description"))
                pub_str = rss_text("pubDate") or rss_text("dc:date")
                pub_date = parse_rss_date(pub_str)
                img = extract_image_from_rss_entry(item, ns)
                articles.append({
                    "title": title, "url": url, "summary": summary,
                    "pub_date": pub_date, "image": img,
                    "source": source["name"], "category": source["category"],
                    "image_fallback": source.get("image_fallback"),
                })
    except ET.ParseError as exc:
        log.warning("RSS parse error for %s: %s", source["name"], exc)

    # Останні 48 год; лише PS / Xbox тематика
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = []
    for a in articles:
        if not a.get("title") or not a.get("url"):
            continue
        if a["pub_date"] and a["pub_date"] < cutoff:
            continue
        if not passes_content_filter(a):
            continue
        a["category"] = classify_rss_category(a["title"], a.get("summary", ""))
        recent.append(a)

    return recent

# ──────────────────────── Giveaway Fetcher ────────────────────────────────────

async def _fetch_gamerpower_platform(
    client: httpx.AsyncClient, api_url: str, platform_label: str,
) -> list[dict]:
    data = await fetch_url(client, api_url)
    if not data:
        return []
    try:
        items = json.loads(data)
        if not isinstance(items, list):
            return []
    except json.JSONDecodeError:
        return []

    pending: list[dict] = []
    for item in items[:8]:
        if item.get("status") != "Active":
            continue
        title    = item.get("title", "")
        desc     = clean_html(item.get("description", ""))
        url      = normalize_article_url(
            item.get("open_giveaway_url") or item.get("giveaway_url") or ""
        )
        image    = item.get("image", "")
        platform = item.get("platforms", "PC")
        worth    = item.get("worth", "")
        end_date = item.get("end_date", "")

        worth_str = f" (варт. {worth})" if worth and worth != "N/A" else ""
        end_str   = f"\n⏰ До: {end_date}" if end_date and end_date != "N/A" else ""

        summary = f"{desc[:300]}{worth_str}{end_str}" if desc else f"{worth_str}{end_str}"

        pending.append({
            "title":    title,
            "url":      url,
            "summary":  summary,
            "image":    image,
            "platform": platform_label,
            "source":   f"GamerPower ({platform_label})",
            "category": "giveaway",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://www.gamerpower.com/images/gamerpower-logo.png",
        })

    giveaways: list[dict] = []
    if pending:
        resolved = await asyncio.gather(
            *[resolve_final_url(client, g["url"]) for g in pending],
            return_exceptions=True,
        )
        for g, final in zip(pending, resolved):
            if isinstance(final, str) and final:
                g["url"] = normalize_store_url(final)
            if g.get("url") and is_ps_xbox_giveaway(g):
                giveaways.append(g)
            elif g.get("url"):
                log.debug("Giveaway skipped (not PS/Xbox store): %s", g.get("title", "")[:50])
            else:
                log.warning("Giveaway skipped (no store URL): %s", g.get("title", "")[:50])

    return giveaways

async def fetch_ps_xbox_giveaways(client: httpx.AsyncClient) -> list[dict]:
    """Безкоштовні ігри лише PlayStation / Xbox."""
    ps, xbox = await asyncio.gather(
        _fetch_gamerpower_platform(client, GAMERPOWER_PS, "PlayStation"),
        _fetch_gamerpower_platform(client, GAMERPOWER_XBOX, "Xbox"),
        return_exceptions=True,
    )
    merged: list[dict] = []
    for batch in (ps, xbox):
        if isinstance(batch, list):
            merged.extend(batch)
        elif isinstance(batch, Exception):
            log.warning("GamerPower error: %s", batch)
    return merged

async def fetch_epic_free_games(client: httpx.AsyncClient) -> list[dict]:
    """Fetch free Epic Games Store games via their public promotions API."""
    url = (
        "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
        "?locale=uk&country=UA&allowCountries=UA"
    )
    data = await fetch_url(client, url)
    if not data:
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []

    games = []
    try:
        elements = payload["data"]["Catalog"]["searchStore"]["elements"]
    except (KeyError, TypeError):
        return []

    for el in elements:
        promos = el.get("promotions") or {}
        offers = promos.get("promotionalOffers", [])
        upcoming = promos.get("upcomingPromotionalOffers", [])
        all_offers = offers + upcoming

        is_free = any(
            offer.get("promotionalOffers", [{}])[0].get("discountSetting", {}).get("discountPercentage", -1) == 0
            for offer in all_offers
            if offer.get("promotionalOffers")
        )
        if not is_free:
            continue

        title = el.get("title", "")
        if "mystery" in title.lower():
            continue
        url   = epic_store_url(el)
        desc  = clean_html(el.get("description", ""))

        image = ""
        for kv in el.get("keyImages", []):
            if kv.get("type") in ("Thumbnail", "DieselStoreFrontWide", "OfferImageWide"):
                image = kv.get("url", "")
                break

        end_str = ""
        for offer in all_offers:
            for promo in offer.get("promotionalOffers", []):
                ed = promo.get("endDate", "")
                if ed:
                    end_str = f"\n⏰ До: {ed[:10]}"
                    break

        games.append({
            "title":    f"🟣 Epic FREE: {title}",
            "url":      url,
            "summary":  f"{desc[:300]}{end_str}",
            "image":    image,
            "source":   "Epic Games Store",
            "category": "giveaway",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://store.epicgames.com/static/images/og-epic-games-store.png",
        })
        if len(games) >= 3:
            break
    return games

async def fetch_steam_free_games(client: httpx.AsyncClient) -> list[dict]:
    """Fetch free-to-play or currently free games from Steam API."""
    url = (
        "https://store.steampowered.com/api/featuredcategories"
        "?cc=ua&l=english"
    )
    data = await fetch_url(client, url)
    if not data:
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []

    games = []
    specials = payload.get("specials", {}).get("items", [])
    for item in specials[:3]:
        if item.get("final_price", 1) != 0:
            continue
        title = item.get("name", "")
        appid = item.get("id", "")
        url   = f"https://store.steampowered.com/app/{appid}/" if appid else "https://store.steampowered.com/specials"
        image = item.get("large_capsule_image") or item.get("small_capsule_image", "")

        games.append({
            "title":    f"🎮 Steam FREE: {title}",
            "url":      url,
            "summary":  "Безкоштовно в Steam — забирай прямо зараз!",
            "image":    image,
            "source":   "Steam",
            "category": "giveaway",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://store.steampowered.com/favicon.ico",
        })
    return games

# ──────────────────────── RAWG Releases Fetcher ───────────────────────────────

async def fetch_rawg_releases(client: httpx.AsyncClient) -> list[dict]:
    """Релізи цього місяця — лише PlayStation / Xbox (RAWG)."""
    if not RAWG_KEY:
        return []
    today = datetime.now(timezone.utc).date()
    in_range = today + timedelta(days=45)  # поточний + наступний місяць
    url = (
        f"https://api.rawg.io/api/games"
        f"?key={RAWG_KEY}"
        f"&dates={today},{in_range}"
        f"&platforms=187,186,1,18"
        f"&ordering=released"
        f"&page_size=15"
    )
    data = await fetch_url(client, url)
    if not data:
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []

    releases = []
    for game in payload.get("results", []):
        plat_names = [
            p["platform"]["name"]
            for p in game.get("platforms", [])
            if p.get("platform")
        ]
        plat_lower = " ".join(plat_names).lower()
        if not any(px in plat_lower for px in RAWG_PS_XBOX):
            continue

        title    = game.get("name", "")
        slug     = game.get("slug", "")
        rel_date = game.get("released", "")
        rating   = game.get("metacritic")
        image    = game.get("background_image", "")
        game_url = f"https://rawg.io/games/{slug}" if slug else "https://rawg.io"

        platforms = ", ".join(plat_names[:4])
        rating_str   = f"⭐ Metacritic: {rating}\n" if rating else ""
        platform_str = f"🎮 Платформи: {platforms}\n" if platforms else ""

        releases.append({
            "title":    f"🚀 Реліз: {title}",
            "url":      game_url,
            "summary":  f"{rating_str}{platform_str}📅 Дата виходу: {rel_date}",
            "image":    image,
            "source":   "RAWG",
            "category": "release",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://rawg.io/apple-touch-icon.png",
        })
    return releases

# ──────────────────────── Hashtags & CTA ────────────────────────────────────────

BASE_HASHTAGS = ("#PlayStation", "#Xbox", "#Gaming")

CTA_UA = (
    "Що думаєш про це? Пиши в коментарях 👇",
    "Чекаєш реліз? Став 🔥",
    "Поділися постом з друзями 🎮",
    "Згоден чи ні? Напиши свою думку 💬",
)
CTA_RU = (
    "Что думаешь об этом? Пиши в комментариях 👇",
    "Ждёшь релиз? Ставь 🔥",
    "Поделись постом с друзьями 🎮",
    "Согласен или нет? Напиши своё мнение 💬",
)

def pick_hashtags(article: dict) -> str:
    """2–4 хештеги (однакові для UA та RU блоків)."""
    tags = list(BASE_HASHTAGS)
    blob = f"{article.get('title', '')} {article.get('url', '')} {article.get('platform', '')}".lower()
    if any(x in blob for x in ("ps plus", "ps+", "playstation plus")):
        tags.append("#PSPlus")
    if any(x in blob for x in ("playstation", "ps4", "ps5", "ps ")):
        tags.append("#PS5")
    if any(x in blob for x in ("game pass", "xbox")):
        tags.append("#GamePass")
    elif "nintendo" in blob or "switch" in blob:
        tags.append("#Nintendo")
    elif "epic" in blob:
        tags.append("#EpicGames")
    elif "steam" in blob:
        tags.append("#Steam")
    return " ".join(tags[:4])

def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."

# ──────────────────────── Gemini: bilingual post template ─────────────────────

_POST_JSON_KEYS = (
    "title_ua", "title_ru", "facts_ua", "facts_ru",
    "description_ua", "description_ru", "opinion_ua", "opinion_ru",
    "cta_ua", "cta_ru", "hashtags",
)

def _gemini_bilingual_post(
    title: str, summary: str, category: str, source: str, url: str,
) -> Optional[dict]:
    """Повертає структурований пост UA+RU для єдиного шаблону."""
    if not GEMINI_KEY:
        return None
    try:
        import google.genai as genai
        import google.genai.types as gtypes
    except ImportError:
        log.warning("google-genai not installed — skipping AI rewrite")
        return None

    cat_hints = {
        "giveaway": "Безкоштовна роздача. Акцент: забери гру безкоштовно, обмежений час.",
        "release":  "Реліз або дата виходу. Вкажи дату та платформи у facts.",
        "update":   "Патч/DLC. У facts — що змінилось.",
        "trailer":  "Трейлер. У facts — гра та платформи.",
        "news":     "Ігрова новина. Коротко та цікаво.",
    }
    hint = cat_hints.get(category, cat_hints["news"])

    prompt = f"""Ти — редактор Telegram-каналу про PlayStation, PS Plus та Xbox / Game Pass.
Створи пост за ЄДИНИМ шаблоном у ДВОХ мовах (спочатку повна українська версія, потім повна російська).
Акцент: підписки, ігри місяця, релізи на PS5/PS4/Xbox — без зайвого PC/Nintendo.

Контекст: {hint}

Оригінал:
Заголовок: {title}
Опис: {summary[:700] if summary else '(немає)'}
Джерело: {source}

Поверни JSON з полями:
- title_ua, title_ru — короткий яскравий заголовок (до 100 символів кожен)
- facts_ua, facts_ru — 2–4 пункти (дата, платформи, жанр, ціна/роздача) через "• "
- description_ua, description_ru — опис новини (2–3 речення)
- opinion_ua, opinion_ru — блок "Твоя думка" / "Твоё мнение": 1–2 речення (оцінка, враження, прогноз або рекомендація)
- ОБОВʼЯЗКОВО заповни ВСІ поля. title_ru має бути російською, не копією англійського заголовка.
- cta_ua, cta_ru — заклик до дії (1 речення, різний від інших постів)
- hashtags — рядок з 2–4 хештегами латиницею/кирилицею (#Ігри #Gaming тощо), однаковий для обох мов

Правила:
- Не вигадуй фактів яких немає в оригіналі.
- Без URL у тексті полів.
- Українська версія — природна українська, російська — природна російська (не дослівний машинний переклад заголовків-суржиком).
- Лаконічно: весь JSON разом до ~1800 символів тексту."""

    try:
        client = genai.Client(api_key=GEMINI_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                max_output_tokens=1400,
                temperature=0.75,
                response_mime_type="application/json",
            ),
        )
        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        article_stub = {"title": title, "url": url, "category": category, "source": source}
        return normalize_post_content(data, article_stub)
    except Exception as exc:
        log.warning("Gemini bilingual post failed: %s", exc)
        return None

def normalize_post_content(content: dict, article: dict) -> dict:
    """Гарантує повний шаблон: CTA, хештеги, RU-поля, «Твоя думка»."""
    out = {k: str(content.get(k) or "").strip() for k in _POST_JSON_KEYS}

    title_ua = out["title_ua"] or article.get("title", "Ігрова новина")
    out["title_ua"] = _clip(title_ua, 120)
    if not out["title_ru"] or out["title_ru"] == out["title_ua"]:
        out["title_ru"] = _clip(f"Игровая новость: {title_ua}", 120)

    if not out["facts_ua"]:
        out["facts_ua"] = f"• Джерело: {article.get('source', '—')}"
    if not out["facts_ru"]:
        out["facts_ru"] = f"• Источник: {article.get('source', '—')}"

    summary = strip_urls_from_text(article.get("summary", "")[:400])
    if not out["description_ua"]:
        out["description_ua"] = summary or "Деталі за посиланням."
    if not out["description_ru"]:
        out["description_ru"] = summary or "Подробности по ссылке."

    if not out["opinion_ua"]:
        out["opinion_ua"] = "На мій погляд, це варта уваги новина для фанатів жанру."
    if not out["opinion_ru"]:
        out["opinion_ru"] = "На мой взгляд, это стоит внимания фанатам жанра."

    out["cta_ua"] = out["cta_ua"] or random.choice(CTA_UA)
    out["cta_ru"] = out["cta_ru"] or random.choice(CTA_RU)
    out["hashtags"] = out["hashtags"] or pick_hashtags(article)
    return out

def is_valid_bilingual_post(text: str) -> bool:
    return (
        "🇺🇦" in text
        and "🇷🇺" in text
        and "Твоя думка" in text
        and "👉" in text
        and "#" in text
    )

def _fallback_bilingual_post(article: dict) -> dict:
    """Шаблон без AI — базова двомовність."""
    title = article.get("title", "Ігрова новина")
    summary = strip_urls_from_text(article.get("summary", "")[:400])
    cat = article.get("category", "news")
    if cat == "giveaway":
        opinion_ua = "Варто забрати, поки безкоштовно — такі роздачі рідко повторюються."
        opinion_ru = "Стоит забрать, пока бесплатно — такие раздачи редко повторяются."
    else:
        opinion_ua = "Цікава новина — варто стежити за розвитком подій."
        opinion_ru = "Интересная новость — стоит следить за развитием событий."

    raw = {
        "title_ua": _clip(title, 100),
        "title_ru": "",
        "facts_ua": "• Джерело: " + article.get("source", "—"),
        "facts_ru": "• Источник: " + article.get("source", "—"),
        "description_ua": summary or "Деталі за посиланням.",
        "description_ru": "Подробности в источнике — смотри ссылку ниже.",
        "opinion_ua": opinion_ua,
        "opinion_ru": opinion_ru,
        "cta_ua": random.choice(CTA_UA),
        "cta_ru": random.choice(CTA_RU),
        "hashtags": pick_hashtags(article),
    }
    return normalize_post_content(raw, article)

def build_bilingual_post(article: dict, content: dict) -> str:
    """Єдиний шаблон: UA блок → роздільник → RU блок → хештеги."""
    url = normalize_store_url(article.get("url", ""))
    cat = article.get("category", "news")
    emoji = CATEGORY_EMOJI.get(cat, "🎮")
    hashtags = (content.get("hashtags") or pick_hashtags(article)).strip()

    ua_block = f"""🇺🇦 Українська версія

{emoji} {content.get('title_ua', '')}

📌 Ключові факти:
{content.get('facts_ua', '—')}

📰 Опис:
{content.get('description_ua', '')}

💭 Твоя думка:
{content.get('opinion_ua', '')}

🔗 Посилання:
{url}

# Хештеги:
{hashtags}

📢 Заклик до дії:
👉 {content.get('cta_ua', random.choice(CTA_UA))}"""

    ru_block = f"""🇷🇺 Русская версия

{emoji} {content.get('title_ru', '')}

📌 Ключевые факты:
{content.get('facts_ru', '—')}

📰 Описание:
{content.get('description_ru', '')}

💭 Твоё мнение:
{content.get('opinion_ru', '')}

🔗 Ссылка:
{url}

# Хештеги:
{hashtags}

📢 Призыв к действию:
👉 {content.get('cta_ru', random.choice(CTA_RU))}"""

    separator = "\n\n" + "─" * 22 + "\n\n"
    text = f"{ua_block}{separator}{ru_block}"

    if len(text) > TEXT_MESSAGE_MAX:
        for key in ("description_ua", "description_ru", "facts_ua", "facts_ru"):
            content[key] = _clip(str(content.get(key, "")), 180)
        return build_bilingual_post(article, content)

    return text

# ──────────────────────── Telegram Poster ─────────────────────────────────────

async def finalize_article_url(client: httpx.AsyncClient, article: dict) -> None:
    """Ensure article URL is the final store/article page before posting."""
    url = normalize_store_url(article.get("url", ""))
    if not url or "gamerpower.com" in url:
        resolved = await resolve_final_url(client, article.get("url", "") or url)
        url = normalize_store_url(resolved)
    article["url"] = url

async def send_post(
    bot: Bot,
    article: dict,
    conn: sqlite3.Connection,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Send a single article as a Telegram post. Returns True on success."""
    if client:
        await finalize_article_url(client, article)

    url   = normalize_store_url(article.get("url", ""))
    title = article.get("title", "")
    if not url:
        log.warning("Skip post without valid URL: %s", title[:60])
        return False

    h = make_hash(url, title)
    if is_posted(conn, h):
        return False

    loop = asyncio.get_event_loop()
    content: Optional[dict] = None
    if GEMINI_KEY:
        try:
            content = await loop.run_in_executor(
                None,
                _gemini_bilingual_post,
                title,
                article.get("summary", ""),
                article.get("category", "news"),
                article.get("source", ""),
                url,
            )
            if content:
                log.info("Gemini bilingual post: %s", title[:60])
        except Exception as exc:
            log.warning("Gemini executor error: %s", exc)

    if not content:
        content = _fallback_bilingual_post(article)
    else:
        content = normalize_post_content(content, article)

    text = build_bilingual_post(article, content)
    if not is_valid_bilingual_post(text):
        log.warning("Invalid bilingual template — using fallback for: %s", title[:60])
        content = _fallback_bilingual_post(article)
        text = build_bilingual_post(article, content)

    image = article.get("image") or article.get("image_fallback") or ""
    send_kw = {"chat_id": CHANNEL_ID}
    full_text = text[:TEXT_MESSAGE_MAX]

    try:
        # Завжди повний пост текстом (UA+RU+CTA) — не обрізаний підпис до фото
        await bot.send_message(
            text=full_text,
            disable_web_page_preview=True,
            **send_kw,
        )
        if image:
            try:
                await bot.send_photo(
                    photo=image,
                    caption=f"🖼 {content.get('title_ua', title)[:100]}",
                    **send_kw,
                )
            except TelegramError as img_exc:
                log.warning("Photo attach failed (text post OK): %s", img_exc)

        mark_posted(conn, h, title, url, article.get("category", "news"), article.get("source", ""))
        log.info(
            "Posted v%s bilingual [%s] %s | %d chars | RU:%s CTA:%s",
            BOT_VERSION,
            article.get("category"),
            title[:50],
            len(full_text),
            "🇷🇺" in full_text,
            "👉" in full_text,
        )
        return True
    except TelegramError as exc:
        log.error("Telegram error posting '%s': %s", title[:60], exc)
        return False

# ──────────────────────── Dedup & Priority Sort ───────────────────────────────

def _dedup_key(a: dict) -> str:
    """Collapse same game from GamerPower + Epic/Steam APIs."""
    url = (a.get("url") or "").lower()
    m = re.search(r"steampowered\.com/app/(\d+)", url)
    if m:
        return f"steam:{m.group(1)}"
    m = re.search(r"epicgames\.com/(?:uk/)?p/([\w\-]+)", url)
    if m and m.group(1) not in ("[]",):
        return f"epic:{m.group(1)}"
    m = re.search(r"gog\.com/game/([\w\-_]+)", url)
    if m:
        return f"gog:{m.group(1)}"
    title_key = re.sub(r"[^a-zA-Z0-9]", "", (a.get("title") or "")).lower()[:50]
    return f"{a.get('category')}:{title_key}"

def deduplicate(articles: list[dict], conn: sqlite3.Connection) -> list[dict]:
    seen_hashes = set()
    seen_keys = set()
    result = []
    for a in articles:
        h = make_hash(a.get("url", ""), a.get("title", ""))
        if is_posted(conn, h):
            continue
        if h in seen_hashes:
            continue
        dkey = _dedup_key(a)
        if dkey in seen_keys:
            continue
        seen_hashes.add(h)
        seen_keys.add(dkey)
        result.append(a)
    return result

# ──────────────────────── Main Polling Loop ───────────────────────────────────

async def collect_all_news(client: httpx.AsyncClient) -> list[dict]:
    """PS/Xbox RSS + релізи; роздачі лише PS/Xbox (вибір у select_articles_for_cycle)."""
    tasks = [fetch_rss(client, source) for source in RSS_SOURCES]
    tasks.append(fetch_ps_xbox_giveaways(client))
    if RAWG_KEY:
        tasks.append(fetch_rawg_releases(client))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    for res in results:
        if isinstance(res, Exception):
            log.warning("Source error: %s", res)
            continue
        if isinstance(res, list):
            all_articles.extend(res)

    return all_articles

async def run_bot():
    if not TOKEN:
        log.error("GAMING_BOT_TOKEN is not set. Add it to .env and restart.")
        return

    conn = init_db()
    bot  = Bot(token=TOKEN)

    try:
        me = await bot.get_me()
        log.info("Bot started v%s: @%s | Channel: %s", BOT_VERSION, me.username, CHANNEL_ID)
    except TelegramError as exc:
        log.error("Cannot connect to Telegram: %s", exc)
        return

    log.info(
        "Limits: news every %dh, max %d/day | giveaway if no news %dh | gv max %d/day",
        NEWS_MIN_POST_GAP // 3600, MAX_POSTS_PER_DAY,
        GIVEAWAY_IF_NO_NEWS_HOURS, MAX_GIVEAWAYS_PER_DAY,
    )
    log.info("Starting polling loop — check interval: %d min", CHECK_INTERVAL // 60)

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=30) as client:
        while True:
            try:
                log.info("Fetching all news sources...")
                articles = await collect_all_news(client)
                articles = deduplicate(articles, conn)

                platform_queued = sum(
                    1 for a in articles
                    if a.get("category") in CONTENT_CATEGORIES and passes_content_filter(a)
                )
                log.info(
                    "Queue: %d total | PS/Xbox content: %d | giveaways: %d",
                    len(articles),
                    platform_queued,
                    sum(1 for a in articles if a.get("category") == "giveaway"),
                )

                hours_since = hours_since_last_post(conn)
                is_fallback = hours_since >= FALLBACK_HOURS

                if hours_since * 3600 < MIN_POST_GAP and not is_fallback:
                    log.info(
                        "Skip cycle: %.1f h since last post (min %d h)",
                        hours_since, MIN_POST_GAP // 3600,
                    )
                else:
                    to_post = select_articles_for_cycle(articles, conn)
                    if is_fallback and not to_post and articles:
                        fallback_pool = [
                            a for a in articles
                            if a.get("category") in CONTENT_CATEGORIES and passes_content_filter(a)
                        ]
                        if fallback_pool:
                            to_post = [sort_platform_content(fallback_pool)[0]]
                            log.info("Fallback: repost best PS/Xbox item from queue")

                    post_count = 0
                    for article in to_post[:MAX_POSTS_PER_CYCLE]:
                        posted = await send_post(bot, article, conn, client)
                        if posted:
                            post_count += 1

                    if post_count == 0:
                        log.info("Nothing to post this cycle")

                log.info("Cycle done. Next check in %d min.", CHECK_INTERVAL // 60)

            except Exception as exc:
                log.error("Unexpected error in main loop: %s", exc, exc_info=True)

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
