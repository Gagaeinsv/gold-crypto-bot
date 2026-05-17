"""
Gaming News Telegram Channel Bot (v1.4.0)
-----------------------------------------
Публікує в Telegram-канал:
  • Ігрові новини (IGN, Kotaku, PC Gamer, Eurogamer та ін.) — пріоритет
  • Безкоштовні ігри з Epic / Steam / GOG / PS / Xbox — рідко, різні магазини
  • Релізи на найближчі 7 днів (RAWG API, опційно)

Ліміти за замовчуванням: 1 пост / 6 год, макс. 4/день, 1 роздача/день.
Налаштування в .env — див. .env.example

Запуск:  python gaming_bot.py
Залежності: pip install -r requirements.txt
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
BOT_VERSION     = "1.4.0"

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default

DB_PATH         = "gaming_bot.db"
CHECK_INTERVAL  = _env_int("CHECK_INTERVAL_MIN", 20) * 60
MIN_POST_GAP    = _env_int("MIN_POST_GAP_HOURS", 6) * 60 * 60
MAX_POSTS_PER_CYCLE = _env_int("MAX_POSTS_PER_CYCLE", 1)
MAX_POSTS_PER_DAY   = _env_int("MAX_POSTS_PER_DAY", 4)
MAX_GIVEAWAYS_PER_DAY  = _env_int("MAX_GIVEAWAYS_PER_DAY", 1)
MAX_GIVEAWAYS_PER_WEEK = _env_int("MAX_GIVEAWAYS_PER_WEEK", 3)
MIN_HOURS_BETWEEN_GIVEAWAYS = _env_int("MIN_HOURS_BETWEEN_GIVEAWAYS", 36)
NEWS_BEFORE_GIVEAWAY = _env_int("NEWS_BEFORE_GIVEAWAY", 3)
FALLBACK_HOURS  = _env_int("FALLBACK_HOURS", 84)
MAX_POST_LENGTH = 1024

# Major stores only for giveaways (skip itch/indiegala flood from aggregators)
GIVEAWAY_STORE_PRIORITY = (
    ("epic",   "epicgames.com"),
    ("steam",  "steampowered.com"),
    ("gog",    "gog.com"),
    ("ps",     "playstation.com"),
    ("xbox",   "xbox.com"),
    ("nintendo", "nintendo.com"),
)
GIVEAWAY_STORE_SKIP = ("itch.io", "indiegala.com", "onstove.com", "gamerpower.com")

# ──────────────────────── RSS News Sources ────────────────────────────────────
RSS_SOURCES = [
    {
        "name": "IGN",
        "url": "https://feeds.ign.com/ign/all",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://assets1.ignimgs.com/2019/06/06/ign-logo-alt-1559862288132.jpg",
    },
    {
        "name": "Eurogamer",
        "url": "https://www.eurogamer.net/feed",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.eurogamer.net/images/2023/08/eurogamer_icon.png",
    },
    {
        "name": "PC Gamer",
        "url": "https://www.pcgamer.com/rss/",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://cdn.mos.cms.futurecdn.net/PCGamerFavicon-16x16.png",
    },
    {
        "name": "Kotaku",
        "url": "https://kotaku.com/rss",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://i.kinja-img.com/gawker-media/image/upload/s--JEQ09gIe--/c_fill,f_auto,fl_progressive,g_center,h_80,q_80,w_80/18j9bkx4d4r1xjpg.jpg",
    },
    {
        "name": "Rock Paper Shotgun",
        "url": "https://www.rockpapershotgun.com/feed",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.rockpapershotgun.com/images/icons/rps-favicon.png",
    },
    {
        "name": "VG247",
        "url": "https://www.vg247.com/feed",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.vg247.com/wp-content/uploads/2023/01/vg247-logo.svg",
    },
    {
        "name": "GamesRadar",
        "url": "https://www.gamesradar.com/rss/",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.gamesradar.com/wp-content/themes/gamesradar/images/gamesradar-logo.png",
    },
    {
        "name": "GameSpot News",
        "url": "https://www.gamespot.com/feeds/mashup/",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.gamespot.com/a/bundles/gamespotsite/images/favicon.ico",
    },
    {
        "name": "Destructoid",
        "url": "https://www.destructoid.com/feed/",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.destructoid.com/wp-content/themes/destructoid/images/logo.png",
    },
    {
        "name": "Nintendo Life",
        "url": "https://www.nintendolife.com/feeds/news",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.nintendolife.com/images/icons/nl_icon.png",
    },
    {
        "name": "Push Square (PlayStation)",
        "url": "https://www.pushsquare.com/feeds/news",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://www.pushsquare.com/images/icons/ps_icon.png",
    },
    {
        "name": "GamingBolt",
        "url": "https://gamingbolt.com/feed",
        "category": "news",
        "lang": "en",
        "image_fallback": "https://gamingbolt.com/wp-content/uploads/2023/01/gamingbolt-logo.png",
    },
]

# ──────────────────────── Giveaway APIs ───────────────────────────────────────
GIVEAWAYRADAR_URL = "https://www.giveawayradar.com/api/giveaways/gaming?count=10"
GAMERPOWER_URL    = "https://www.gamerpower.com/api/giveaways?platform=pc&type=game&sort-by=date"

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
    News-first scheduling: usually one news item; giveaways rarely, rotated across stores.
    """
    if count_posts_since(conn, 24) >= MAX_POSTS_PER_DAY:
        log.info("Daily post cap reached (%d)", MAX_POSTS_PER_DAY)
        return []

    news     = [a for a in articles if a.get("category") == "news"]
    releases = [a for a in articles if a.get("category") == "release"]
    giveaways = [a for a in articles if a.get("category") == "giveaway"]

    # News and releases: newest first
    def by_date(a):
        pd = a.get("pub_date")
        return pd.timestamp() if pd else 0

    news.sort(key=by_date, reverse=True)
    releases.sort(key=by_date, reverse=True)

    # Giveaway only after at least 3 news posts since the last giveaway
    last_gv = conn.execute(
        "SELECT MAX(posted_at) FROM posted WHERE category = 'giveaway'"
    ).fetchone()[0]
    news_after_giveaway = 999
    if last_gv:
        row = conn.execute(
            "SELECT COUNT(*) FROM posted WHERE category = 'news' AND posted_at > ?",
            (last_gv,),
        ).fetchone()
        news_after_giveaway = int(row[0]) if row else 0

    want_giveaway = (
        can_post_giveaway(conn)
        and giveaways
        and news_after_giveaway >= NEWS_BEFORE_GIVEAWAY
    )

    if want_giveaway:
        pick = pick_diverse_giveaway(giveaways, conn)
        if pick:
            log.info("Selected giveaway from store: %s", giveaway_store_key(pick.get("url", "")))
            return [pick]

    if news:
        return [news[0]]
    if releases:
        return [releases[0]]

    # Last resort: giveaway if allowed and nothing else
    if can_post_giveaway(conn) and giveaways:
        pick = pick_diverse_giveaway(giveaways, conn)
        if pick:
            return [pick]

    return []

# ──────────────────────── Helpers ─────────────────────────────────────────────

def make_hash(*parts: str) -> str:
    text = "|".join(str(p) for p in parts)
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def truncate(text: str, limit: int = MAX_POST_LENGTH) -> str:
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

    # Filter to last 48 hours (generous window to not miss slower days)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = []
    for a in articles:
        if not a.get("title") or not a.get("url"):
            continue
        if a["pub_date"] and a["pub_date"] < cutoff:
            continue
        if not is_gaming_related(a["title"], a.get("summary", "")):
            continue
        recent.append(a)

    return recent

# ──────────────────────── Giveaway Fetcher ────────────────────────────────────

async def fetch_gamerpower_giveaways(client: httpx.AsyncClient) -> list[dict]:
    """Fetch free game giveaways from GamerPower API (free, no key needed)."""
    data = await fetch_url(client, GAMERPOWER_URL)
    if not data:
        return []
    try:
        items = json.loads(data)
        if not isinstance(items, list):
            return []
    except json.JSONDecodeError:
        return []

    giveaways = []
    pending: list[dict] = []
    for item in items[:12]:
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
            "platform": platform.lower(),
            "source":   "GamerPower",
            "category": "giveaway",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://www.gamerpower.com/images/gamerpower-logo.png",
        })

    # Resolve GamerPower redirect pages → direct Steam / Epic / itch.io links
    if pending:
        resolved = await asyncio.gather(
            *[resolve_final_url(client, g["url"]) for g in pending],
            return_exceptions=True,
        )
        for g, final in zip(pending, resolved):
            if isinstance(final, str) and final:
                g["url"] = normalize_store_url(final)
            if g.get("url"):
                giveaways.append(g)
            else:
                log.warning("Giveaway skipped (no store URL): %s", g.get("title", "")[:50])

    return giveaways

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
    """Fetch upcoming/new game releases from RAWG API."""
    if not RAWG_KEY:
        return []
    today    = datetime.now(timezone.utc).date()
    in_7days = today + timedelta(days=7)
    url = (
        f"https://api.rawg.io/api/games"
        f"?key={RAWG_KEY}"
        f"&dates={today},{in_7days}"
        f"&ordering=-added"
        f"&page_size=10"
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
        title    = game.get("name", "")
        slug     = game.get("slug", "")
        rel_date = game.get("released", "")
        rating   = game.get("metacritic")
        image    = game.get("background_image", "")
        url      = f"https://rawg.io/games/{slug}" if slug else "https://rawg.io"

        platforms = ", ".join(
            p["platform"]["name"] for p in game.get("platforms", [])[:4]
        )
        rating_str   = f"⭐ Metacritic: {rating}\n" if rating else ""
        platform_str = f"🖥️ Платформи: {platforms}\n" if platforms else ""

        releases.append({
            "title":    f"🚀 Реліз: {title}",
            "url":      url,
            "summary":  f"{rating_str}{platform_str}📅 Дата виходу: {rel_date}",
            "image":    image,
            "source":   "RAWG",
            "category": "release",
            "pub_date": datetime.now(timezone.utc),
            "image_fallback": "https://rawg.io/apple-touch-icon.png",
        })
    return releases

# ──────────────────────── Gemini AI Rewriter ─────────────────────────────────

def _gemini_rewrite(title: str, summary: str, category: str, source: str, url: str) -> Optional[str]:
    """
    Use Gemini Flash to rewrite an article into an engaging Ukrainian Telegram post.
    Returns the rewritten text, or None on failure (caller falls back to plain format).
    """
    if not GEMINI_KEY:
        return None
    try:
        import google.genai as genai
        import google.genai.types as gtypes
    except ImportError:
        log.warning("google-genai not installed — skipping AI rewrite")
        return None

    cat_hints = {
        "giveaway": "Це безкоштовна роздача гри. Зроби акцент на тому, що гра безкоштовна, і що треба поспішати.",
        "release":  "Це новина про реліз або дату виходу гри. Підкресли дату і платформи.",
        "update":   "Це патч або оновлення гри. Коротко перелічи найцікавіше що додали/виправили.",
        "news":     "Це загальна ігрова новина. Подай її захопливо.",
    }
    hint = cat_hints.get(category, cat_hints["news"])

    prompt = f"""Ти — редактор україномовного Telegram-каналу про відеоігри.
Твоє завдання: перетворити нижченаведену англомовну новину на короткий, живий пост УКРАЇНСЬКОЮ мовою для Telegram-каналу.

Правила:
1. Пиши виключно УКРАЇНСЬКОЮ мовою.
2. Починай одразу з суті — без вступних фраз типу "Ось новина" або "Привіт".
3. Обсяг: 2–4 речення (максимум 300 символів тексту без заголовку).
4. Додавай 1–2 доречних емодзі в тексті — не переборщуй.
5. НЕ додавай посилань (жодних URL), хештегів, підписів "#реклама" чи будь-яких HTML-тегів — тільки чистий текст.
6. НЕ вигадуй деталей яких немає в оригіналі.
7. {hint}

Оригінальний заголовок: {title}
Короткий опис: {summary[:600] if summary else '(немає)'}
Джерело: {source}

Виведи ТІЛЬКИ готовий текст посту українською — без жодних пояснень, заголовків чи обгортки."""

    try:
        client = genai.Client(api_key=GEMINI_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                max_output_tokens=400,
                temperature=0.7,
            ),
        )
        text = (response.text or "").strip()
        if len(text) < 20:
            return None
        return text
    except Exception as exc:
        log.warning("Gemini rewrite failed: %s", exc)
        return None


# ──────────────────────── Message Formatter ───────────────────────────────────

def format_post(article: dict, ai_body: Optional[str] = None) -> str:
    """
    Plain-text caption with URL on its own line (Telegram auto-linkifies it).
    No HTML — avoids broken <a> tags; no reliance on inline buttons in channels.
    """
    cat      = article.get("category", "news")
    emoji    = CATEGORY_EMOJI.get(cat, "🎮")
    source   = article.get("source", "")
    title    = article.get("title", "Без назви")
    url      = normalize_store_url(article.get("url", ""))

    platform = article.get("platform", "")
    plat_em  = PLATFORM_EMOJI.get(platform, "") if platform else ""

    raw_body = ai_body if ai_body else (article.get("summary", "")[:600] if article.get("summary") else "")
    body     = strip_urls_from_text(raw_body)

    link_block = f"\n\n🔗 Посилання:\n{url}" if url else ""
    src_block  = f"\n\n📰 Джерело: {source}" if source else ""
    header     = f"{emoji}{plat_em} {title}\n\n"

    budget = MAX_POST_LENGTH - len(header) - len(link_block) - len(src_block) - 4
    if body and budget > 20 and len(body) > budget:
        body = body[: budget - 3].rstrip() + "..."

    text = header + (body + link_block + src_block if body else link_block.lstrip() + src_block)
    return text[:MAX_POST_LENGTH]

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

    # Run Gemini rewrite in a thread (blocking SDK call) so we don't block the event loop
    ai_body: Optional[str] = None
    if GEMINI_KEY:
        loop = asyncio.get_event_loop()
        try:
            ai_body = await loop.run_in_executor(
                None,
                _gemini_rewrite,
                title,
                article.get("summary", ""),
                article.get("category", "news"),
                article.get("source", ""),
                url,
            )
            if ai_body:
                log.info("Gemini rewrote: %s", title[:60])
        except Exception as exc:
            log.warning("Gemini executor error: %s", exc)

    if ai_body:
        ai_body = strip_urls_from_text(ai_body)

    caption = format_post(article, ai_body=ai_body)
    image   = article.get("image") or article.get("image_fallback") or ""
    # Plain text + raw URL line — most reliable in Telegram channels
    send_kw = {"chat_id": CHANNEL_ID}

    try:
        if image:
            await bot.send_photo(photo=image, caption=caption, **send_kw)
        else:
            await bot.send_message(text=caption, disable_web_page_preview=True, **send_kw)
        mark_posted(conn, h, title, url, article.get("category", "news"), article.get("source", ""))
        log.info("Posted: [%s] %s | link: %s", article.get("category"), title[:60], url[:100])
        return True
    except TelegramError as exc:
        log.error("Telegram error posting '%s': %s", title[:60], exc)
        if image:
            try:
                await bot.send_message(text=caption, disable_web_page_preview=True, **send_kw)
                mark_posted(conn, h, title, url, article.get("category", "news"), article.get("source", ""))
                return True
            except TelegramError as exc2:
                log.error("Retry without image also failed: %s", exc2)
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
    """Fetch all sources concurrently and return merged article list."""
    tasks = []

    # RSS feeds
    for source in RSS_SOURCES:
        tasks.append(fetch_rss(client, source))

    # Giveaways: aggregator + official store APIs (deduped later)
    tasks.append(fetch_gamerpower_giveaways(client))
    tasks.append(fetch_epic_free_games(client))
    tasks.append(fetch_steam_free_games(client))

    # Upcoming releases
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
        "Limits: %dh between posts, max %d/day, giveaways max %d/day %d/week",
        MIN_POST_GAP // 3600, MAX_POSTS_PER_DAY, MAX_GIVEAWAYS_PER_DAY, MAX_GIVEAWAYS_PER_WEEK,
    )
    log.info("Starting polling loop — check interval: %d min", CHECK_INTERVAL // 60)

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=30) as client:
        while True:
            try:
                log.info("Fetching all news sources...")
                articles = await collect_all_news(client)
                articles = deduplicate(articles, conn)

                log.info(
                    "Queue after dedup: %d total (%d news, %d giveaway, %d release)",
                    len(articles),
                    sum(1 for a in articles if a.get("category") == "news"),
                    sum(1 for a in articles if a.get("category") == "giveaway"),
                    sum(1 for a in articles if a.get("category") == "release"),
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
                        # Guarantee min 2 posts/week — pick best news
                        news = [a for a in articles if a.get("category") == "news"]
                        to_post = [news[0] if news else articles[0]]
                        log.info("Fallback post (nothing new in queue)")

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
