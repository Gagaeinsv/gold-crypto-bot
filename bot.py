
def fix_markdown(text: str) -> str:
    if not text:
        return ""
    for ch in ("*", "_", "`"):
        if text.count(ch) % 2 != 0:
            text += ch
    return text

"""
Gold & Crypto AI Signals Bot
Run: python bot.py
Dependencies: pip install python-telegram-bot[job-queue] requests groq google-genai pillow yfinance pandas pandas-ta python-dotenv
Env: GROQ_MODEL_* ; OpenRouter key pool (`OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_2`, optional `OPENROUTER_API_KEYS`) ;
     round-robin + automatic failover on 429 ; GEMINI / MONITOR_INTERVAL_SEC / providers — see .env.example.
"""

import asyncio
import base64
import concurrent.futures
import csv
import io
import json
import hashlib
import hmac
import logging
import os
import random
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ─────────────────────── Logging ────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")

# ─────────────────────── Config from .env ───────────────────────
load_dotenv()

TOKEN        = os.getenv("TOKEN",        "INSERT_TOKEN")
NEWS_API     = os.getenv("NEWS_API",     "INSERT_NEWS_API")
GROQ_KEY     = os.getenv("GROQ_KEY",     "INSERT_GROQ_KEY")
# OpenRouter — Groq fallback + optional deep/chart (OpenAI-compatible API).
# Multiple keys: round-robin per request; on 429/quota — automatic failover to next key.
OPENROUTER_API_KEY     = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_KEY_2   = os.getenv("OPENROUTER_API_KEY_2", "").strip()
OPENROUTER_API_KEYS    = os.getenv("OPENROUTER_API_KEYS", "").strip()  # optional comma-separated extra keys
OPENROUTER_MODEL       = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "")  # if empty, uses OPENROUTER_MODEL
OPENROUTER_SITE_URL    = os.getenv("OPENROUTER_SITE_URL", "")   # optional HTTP-Referer for OpenRouter rankings
OPENROUTER_APP_TITLE   = os.getenv("OPENROUTER_APP_TITLE", "Gold Crypto Trading Bot")
OPENROUTER_API_URL     = "https://openrouter.ai/api/v1/chat/completions"


def _openrouter_key_pool() -> list[str]:
    keys: list[str] = []
    for k in (OPENROUTER_API_KEY, OPENROUTER_API_KEY_2):
        if k and k not in keys:
            keys.append(k)
    for part in OPENROUTER_API_KEYS.split(","):
        p = part.strip()
        if p and p not in keys:
            keys.append(p)
    return keys


_OPENROUTER_KEYS: list[str] = _openrouter_key_pool()
_openrouter_rr_lock = threading.Lock()
_openrouter_rr_i = 0


def _openrouter_configured() -> bool:
    return bool(_OPENROUTER_KEYS)
# Google Gemini — deep analysis, chart vision, Groq 429 fallback (works alongside OpenRouter)
GEMINI_KEY   = os.getenv("GEMINI_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# gemini | openrouter | auto  (auto prefers Gemini when GEMINI_KEY is set)
DEEP_ANALYSIS_PROVIDER = os.getenv("DEEP_ANALYSIS_PROVIDER", "gemini").strip().lower()
CHART_VISION_PROVIDER  = os.getenv("CHART_VISION_PROVIDER", "gemini").strip().lower()
GOLD_API_KEY = os.getenv("GOLD_API_KEY", "")   # goldapi.io — spot price for XAU/XAG
NOWPAYMENTS_API_KEY   = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_PAY_CURRENCY = (os.getenv("NOWPAYMENTS_PAY_CURRENCY", "usdttrc20").strip().lower() or "usdttrc20")
# Public base URL of your server, used for NOWPayments IPN callback (must resolve for their servers).
# NOWPayments often rejects http:// callbacks with HTTP 400; use HTTPS behind nginx/Caddy/Certbot.
PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "")
if NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL.strip() and PUBLIC_BASE_URL.lower().startswith("http://"):
    log.warning(
        "NOWPayments: PUBLIC_BASE_URL uses http:// — the API commonly rejects non-HTTPS ipn_callback_url "
        "(400 Bad Request). Use https:// behind a reverse proxy (port 443) and update PUBLIC_BASE_URL."
    )
ADMIN_ID     = int(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID   = os.getenv("CHANNEL_ID",  "@your_channel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@your_bot")

# Groq: use a high-quota model for channel + articles vs a stronger model for per-user signals
# (free tier: 8b ≈ 14k req/day, 70b ≈ 1k req/day — see Groq dashboard).
GROQ_MODEL_NEWS    = os.getenv("GROQ_MODEL_NEWS", "llama-3.1-8b-instant")
GROQ_MODEL_SIGNALS = os.getenv("GROQ_MODEL_SIGNALS", "llama-3.3-70b-versatile")
_LEGACY_GROQ_MODEL  = os.getenv("GROQ_MODEL", "").strip()
if _LEGACY_GROQ_MODEL:
    log.warning(
        "GROQ_MODEL is deprecated; set GROQ_MODEL_NEWS (channel/articles) and "
        "GROQ_MODEL_SIGNALS (user analysis / auto-signals). Using GROQ_MODEL=%s for signals only.",
        _LEGACY_GROQ_MODEL,
    )
    GROQ_MODEL_SIGNALS = _LEGACY_GROQ_MODEL
GROQ_TIMEOUT = 20

# Long-form deep analysis & chart vision: see DEEP_ANALYSIS_PROVIDER / CHART_VISION_PROVIDER (default: gemini).

TRIAL_DAYS        = 7
PRICE_BASIC       = 550    # ~$5 net after Telegram 30% fee
PRICE_PRO         = 1100   # ~$9.99 net
PRICE_BASIC_3     = 1375   # 3-month ~17% discount
PRICE_PRO_3       = 2750
PRICE_DIAMOND     = 2150   # ~$19.99/mo
PRICE_DIAMOND_3   = 5375   # 3mo ~17% off (~$49.99)

# USD list prices used for crypto payments (NOWPayments)
USD_BASIC_1     = 5.00
USD_BASIC_3     = 12.50
USD_PRO_1       = 9.99
USD_PRO_3       = 25.00
USD_DIAMOND_1   = 19.99
USD_DIAMOND_3   = 49.99
# NOWPayments/crypto-network minimums: only these bundles reliably clear without bogus surcharges.
CRYPTO_PAY_ALLOWED = frozenset({("basic", 3), ("pro", 3), ("diamond", 1), ("diamond", 3)})
DB_PATH           = "users.db"
CHANNEL_HOURS_UTC = [6, 12, 18]   # market analysis posts (UTC)
ARTICLE_HOURS_UTC = [8, 14, 20]   # article posts — separate from analysis
# Background job interval (seconds). Channel Groq runs only when the hour hits
# CHANNEL_HOURS_UTC / ARTICLE_HOURS_UTC — not on every tick.
MONITOR_INTERVAL_SEC = max(15, int(os.getenv("MONITOR_INTERVAL_SEC", "60")))
AUTO_COOLDOWN     = 30 * 60        # seconds between auto-signals

PAIRS: dict = {
    # ── Metals ──────────────────────────────────────────────────
    "XAUUSD": {
        "name": "XAU/USD", "emoji": "🥇", "yahoo": "GC=F", "stooq": "xauusd",
        "news_q": "gold USD Fed XAU inflation",
        "sl_pct": 2.0, "tp_pct": 3.0,
        "plans": ["trial", "basic", "pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1610375461246-83df859d849d?w=800&q=80",
    },
    "XAGUSD": {
        "name": "XAG/USD", "emoji": "🥈", "yahoo": "SI=F", "stooq": "xagusd",
        "news_q": "silver XAG price industrial demand",
        "sl_pct": 2.5, "tp_pct": 4.0,
        "plans": ["basic", "pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1569025743873-ea3a9ade89f9?w=800&q=80",
    },
    # ── Top Crypto ───────────────────────────────────────────────
    "BTCUSD": {
        "name": "BTC/USD", "emoji": "₿", "yahoo": "BTC-USD", "stooq": "btcusd",
        "news_q": "bitcoin BTC crypto halving ETF",
        "sl_pct": 3.0, "tp_pct": 5.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1518546305927-5a555bb7020d?w=800&q=80",
    },
    "ETHUSD": {
        "name": "ETH/USD", "emoji": "Ξ", "yahoo": "ETH-USD", "stooq": "ethusd",
        "news_q": "ethereum ETH crypto DeFi upgrade",
        "sl_pct": 3.5, "tp_pct": 6.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1622630998477-20aa696ecb05?w=800&q=80",
    },
    "SOLUSD": {
        "name": "SOL/USD", "emoji": "◎", "yahoo": "SOL-USD", "stooq": "solusd",
        "news_q": "Solana SOL crypto network ecosystem",
        "sl_pct": 4.0, "tp_pct": 7.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800&q=80",
    },
    "XRPUSD": {
        "name": "XRP/USD", "emoji": "✕", "yahoo": "XRP-USD", "stooq": "xrpusd",
        "news_q": "XRP Ripple SEC crypto payment",
        "sl_pct": 4.0, "tp_pct": 7.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800&q=80",
    },
    "BNBUSD": {
        "name": "BNB/USD", "emoji": "🔶", "yahoo": "BNB-USD", "stooq": "bnbusd",
        "news_q": "BNB Binance crypto exchange",
        "sl_pct": 3.5, "tp_pct": 6.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800&q=80",
    },
    "ADAUSD": {
        "name": "ADA/USD", "emoji": "🔵", "yahoo": "ADA-USD", "stooq": "adausd",
        "news_q": "Cardano ADA crypto blockchain",
        "sl_pct": 4.5, "tp_pct": 8.0,
        "plans": ["pro", "diamond", "admin"],
        "image": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800&q=80",
    },
}
DEFAULT_PAIR = "XAUUSD"

UNSPLASH_IMAGES = {
    "gold":        "https://images.unsplash.com/photo-1610375461246-83df859d849d?w=800&q=80",
    "bitcoin":     "https://images.unsplash.com/photo-1518546305927-5a555bb7020d?w=800&q=80",
    "ethereum":    "https://images.unsplash.com/photo-1622630998477-20aa696ecb05?w=800&q=80",
    "fed":         "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800&q=80",
    "crypto":      "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800&q=80",
    "inflation":   "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800&q=80",
    "rsi":         "https://images.unsplash.com/photo-1551288049-bebda4e38f71?w=800&q=80",
    "macd":        "https://images.unsplash.com/photo-1642790106117-e829e14a795f?w=800&q=80",
    "psychology":  "https://images.unsplash.com/photo-1559526324-593bc073d938?w=800&q=80",
    "candlestick": "https://images.unsplash.com/photo-1642790106117-e829e14a795f?w=800&q=80",
    "default_news":"https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800&q=80",
    "default_edu": "https://images.unsplash.com/photo-1551288049-bebda4e38f71?w=800&q=80",
}


def _pick_unsplash(topic_type: str, topic: str) -> str:
    t = topic.lower()
    for kw, url in UNSPLASH_IMAGES.items():
        if kw in t:
            return url
    return UNSPLASH_IMAGES[f"default_{topic_type}"]

_monitor_lock = asyncio.Lock()

# ── In-memory caches ─────────────────────────────────────────────
_price_cache: dict = {}   # pair → (price, timestamp)
_news_cache:  dict = {}   # pair → (news_str, timestamp)
_PRICE_CACHE_TTL = 45     # seconds — refresh price every 45s max
_NEWS_CACHE_TTL  = 300    # seconds — refresh news every 5 minutes

# ═══════════════════════════════════════════════════════════════════
#  Database
# ═══════════════════════════════════════════════════════════════════

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init() -> None:
    with db_connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id          INTEGER PRIMARY KEY,
                username         TEXT,
                first_name       TEXT,
                plan             TEXT    DEFAULT 'trial',
                trial_ends       TEXT,
                sub_expires      TEXT,
                total_paid_stars INTEGER DEFAULT 0,
                joined_at        TEXT    DEFAULT (datetime('now')),
                last_active      TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id            INTEGER,
                stars              INTEGER,
                plan               TEXT,
                months             INTEGER,
                paid_at            TEXT DEFAULT (datetime('now')),
                telegram_charge_id TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pair       TEXT,
                post_type  TEXT,
                score      INTEGER,
                sentiment  TEXT,
                price      REAL,
                posted_at  TEXT DEFAULT (datetime('now')),
                message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS active_trades (
                chat_id           INTEGER,
                pair              TEXT,
                entry_price       REAL,
                waiting_price     REAL,
                sl_warning_sent   INTEGER DEFAULT 0,
                last_signal_time  REAL    DEFAULT 0,
                last_signal_score INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, pair)
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                source      TEXT    DEFAULT 'ref',
                bonus_given INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS utm_sources (
                chat_id    INTEGER PRIMARY KEY,
                source     TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pair         TEXT    NOT NULL,
                direction    TEXT    NOT NULL,
                entry_price  REAL    NOT NULL,
                sl_price     REAL    NOT NULL,
                tp_price     REAL    NOT NULL,
                score        INTEGER DEFAULT 0,
                sentiment    TEXT    DEFAULT 'neutral',
                source       TEXT    DEFAULT 'ai',
                posted_at    TEXT    DEFAULT (datetime('now')),
                resolved_at  TEXT,
                outcome      TEXT,
                pnl_pct      REAL,
                message_id   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deep_analysis_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                pair       TEXT    NOT NULL,
                used_at    TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS crypto_payments (
                payment_id     TEXT    PRIMARY KEY,
                chat_id        INTEGER NOT NULL,
                plan           TEXT    NOT NULL,
                months         INTEGER NOT NULL,
                price_usd      REAL    NOT NULL,
                pay_currency   TEXT    NOT NULL,
                pay_amount     REAL,
                pay_address    TEXT,
                status         TEXT    DEFAULT 'waiting',
                created_at     TEXT    DEFAULT (datetime('now')),
                updated_at     TEXT
            );
        """)


def db_upsert_user(cid: int, username: str = "", fname: str = "") -> None:
    with db_connect() as c:
        row = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (cid,)).fetchone()
        if row is None:
            trial_ends = (datetime.now(UTC) + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
            c.execute(
                "INSERT INTO users(chat_id,username,first_name,plan,trial_ends,last_active) "
                "VALUES(?,?,?,'trial',?,datetime('now'))",
                (cid, username, fname, trial_ends),
            )
        else:
            c.execute(
                "UPDATE users SET last_active=datetime('now'),username=?,first_name=? WHERE chat_id=?",
                (username, fname, cid),
            )


def db_access(cid: int) -> dict:
    with db_connect() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id=?", (cid,)).fetchone()
    if row is None:
        return {"allowed": False, "plan": "none", "days_left": 0, "reason": "not_registered"}

    today = datetime.now(UTC).date()
    plan  = row["plan"]

    if cid == ADMIN_ID:
        return {"allowed": True, "plan": "admin", "days_left": 9999, "reason": ""}

    if plan in ("basic", "pro", "diamond") and row["sub_expires"]:
        exp = datetime.strptime(row["sub_expires"], "%Y-%m-%d").date()
        if exp >= today:
            return {"allowed": True, "plan": plan, "days_left": (exp - today).days, "reason": ""}
        with db_connect() as c:
            c.execute("UPDATE users SET plan='expired' WHERE chat_id=?", (cid,))
        return {"allowed": False, "plan": "expired", "days_left": 0, "reason": "expired"}

    if plan == "trial" and row["trial_ends"]:
        te = datetime.strptime(row["trial_ends"], "%Y-%m-%d").date()
        if te >= today:
            return {"allowed": True, "plan": "trial", "days_left": (te - today).days, "reason": ""}
        with db_connect() as c:
            c.execute("UPDATE users SET plan='expired' WHERE chat_id=?", (cid,))
        return {"allowed": False, "plan": "expired", "days_left": 0, "reason": "trial_ended"}

    return {"allowed": False, "plan": plan, "days_left": 0, "reason": "no_subscription"}


DEEP_ANALYSIS_DAILY_LIMIT = 3  # per user per day

def db_deepanalysis_count_today(cid: int) -> int:
    """Return how many deep analyses this user has used today (UTC)."""
    today = datetime.now(UTC).date().isoformat()
    with db_connect() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM deep_analysis_log "
            "WHERE chat_id=? AND date(used_at)=?",
            (cid, today),
        ).fetchone()
    return row[0] if row else 0


def db_deepanalysis_log(cid: int, pair: str) -> None:
    """Record one deep analysis use."""
    with db_connect() as c:
        c.execute(
            "INSERT INTO deep_analysis_log(chat_id, pair) VALUES(?,?)",
            (cid, pair),
        )


def db_apply_payment(cid: int, stars: int, plan_key: str, months: int, charge_id: str) -> date:
    today = datetime.now(UTC).date()
    with db_connect() as c:
        row = c.execute("SELECT sub_expires FROM users WHERE chat_id=?", (cid,)).fetchone()
        base = (
            max(datetime.strptime(row["sub_expires"], "%Y-%m-%d").date(), today)
            if row and row["sub_expires"]
            else today
        )
        new_exp = base + timedelta(days=30 * months)
        c.execute(
            "UPDATE users SET plan=?,sub_expires=?,total_paid_stars=total_paid_stars+? WHERE chat_id=?",
            (plan_key, new_exp.strftime("%Y-%m-%d"), stars, cid),
        )
        c.execute(
            "INSERT INTO payments(chat_id,stars,plan,months,telegram_charge_id) VALUES(?,?,?,?,?)",
            (cid, stars, plan_key, months, charge_id),
        )
    return new_exp


# ── Payment idempotency helpers ───────────────────────────────────

def db_payment_exists(charge_id: str) -> bool:
    with db_connect() as c:
        row = c.execute(
            "SELECT 1 FROM payments WHERE telegram_charge_id=? LIMIT 1",
            (charge_id,),
        ).fetchone()
    return row is not None


# ── NOWPayments (crypto) payment helpers ──────────────────────────

NOWPAYMENTS_BASE = "https://api.nowpayments.io/v1"


def _nowp_response_error_detail(r: requests.Response) -> str:
    """Best-effort body summary for NOWPayments HTTP errors."""
    txt = ""
    try:
        j = r.json()
        if isinstance(j, dict):
            parts: list[str] = []
            for key in ("message", "code", "error"):
                v = j.get(key)
                if v not in (None, ""):
                    parts.append(str(v))
            if parts:
                return " ".join(parts)
            txt = json.dumps(j, ensure_ascii=False)
    except Exception:
        txt = (r.text or "").strip()
    if not txt.strip():
        return f"HTTP {r.status_code}"
    txt = txt.strip()
    return txt[:800] + ("…" if len(txt) > 800 else "")


def _nowp_headers() -> dict:
    return {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }


def _nowp_first_float(d: dict, keys: tuple[str, ...]) -> float | None:
    """First parsable numeric among common NOWPayments JSON field names."""
    for key in keys:
        raw = d.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _nowp_body_is_hard_fail(body: dict) -> bool:
    """NOWPayments mixes success payloads and `{status:false,...}` shapes."""
    if body.get("status") is False:
        return True
    sc = body.get("statusCode")
    try:
        if sc is not None and int(float(sc)) >= 400:
            return True
    except (TypeError, ValueError):
        pass
    st = body.get("status")
    if isinstance(st, str) and st.strip().lower() in ("false", "fail", "failed", "error"):
        return True
    return False


def _nowp_http_get_body(path_relative: str, params: dict) -> dict | None:
    """GET `/v1/{path}` → parsed JSON dict, or None on transport / unreadable payloads."""
    url = f"{NOWPAYMENTS_BASE}/{path_relative.lstrip('/')}"
    try:
        r = requests.get(url, headers=_nowp_headers(), params=params, timeout=12)
    except requests.RequestException as e:
        log.warning("NOWPayments GET %s transport error: %s", path_relative, e)
        return None
    try:
        body = r.json()
    except ValueError:
        log.warning(
            "NOWPayments GET %s non-JSON HTTP %s: %s",
            path_relative,
            r.status_code,
            (r.text or "")[:220],
        )
        return None
    if not isinstance(body, dict):
        return None
    ok_http = bool(r.ok)
    if ok_http:
        if _nowp_body_is_hard_fail(body):
            detail_parts = []
            for k in ("message", "code", "error"):
                v = body.get(k)
                if v not in (None, ""):
                    detail_parts.append(str(v))
            detail = " ".join(detail_parts) if detail_parts else repr(body)[:260]
            log.warning(
                "NOWPayments GET %s HTTP OK but payload looks like error: %s",
                path_relative,
                detail[:300],
            )
            return None
        return body

    detail_parts = []
    for k in ("message", "code", "error"):
        v = body.get(k)
        if v not in (None, ""):
            detail_parts.append(str(v))
    detail = " ".join(detail_parts) if detail_parts else f"HTTP {r.status_code}"
    log.warning(
        "NOWPayments GET %s refused: HTTP %s %s",
        path_relative,
        r.status_code,
        detail[:300],
    )
    return None


def nowp_minimum_pay_crypto() -> float | None:
    """Minimum payout size in **`pay_currency`** (from `/min-amount`)."""
    j = _nowp_http_get_body(
        "min-amount",
        {"currency_from": "usd", "currency_to": NOWPAYMENTS_PAY_CURRENCY},
    )
    if not j:
        return None
    return _nowp_first_float(j, ("min_amount", "minAmount"))


def nowp_estimate_pay_crypto_for_usd(price_usd: float) -> float | None:
    """Estimate how much **`pay_currency`** user pays for a USD-denominated list price."""
    path = "estimate"
    params = {
        "amount": round(float(price_usd), 10),
        "currency_from": "usd",
        "currency_to": NOWPAYMENTS_PAY_CURRENCY,
    }

    url = f"{NOWPAYMENTS_BASE}/{path.lstrip('/')}"
    try:
        r = requests.get(url, headers=_nowp_headers(), params=params, timeout=12)
    except requests.RequestException as e:
        log.warning("NOWPayments GET %s transport error: %s", path, e)
        return None

    try:
        j = r.json()
    except ValueError:
        log.warning("NOWPayments GET %s non-JSON HTTP %s", path, r.status_code)
        return None
    if not isinstance(j, dict):
        return None

    estimate = _nowp_first_float(j, ("estimated_amount", "estimatedAmount"))
    # Success payloads look like `{ currency_from, currency_to, amount_from?, estimated_amount }`
    # and usually omit `"status"` entirely.
    if estimate is None or _nowp_body_is_hard_fail(j) or not bool(r.ok):
        dp: list[str] = []
        for k in ("message", "code", "error"):
            v = j.get(k)
            if v not in (None, ""):
                dp.append(str(v))
        detail = " ".join(dp) if dp else ((r.text or "")[:240])
        log.warning(
            "NOWPayments GET estimate unusable (HTTP %s): %s",
            r.status_code,
            detail[:320],
        )
        return None

    return estimate


def nowp_crypto_invoice_ceiling_usd(list_price_usd: float) -> float | None:
    """
    HARD cap above list price shown in Telegram so NOWPayments probing cannot explode totals.
    Set NOWP_CRYPTO_DISABLE_INVOICE_CAP=1 on your own risk.
    effective_invoice <= list_price + max(ABS, list_price * pct/100).
    """
    if os.getenv("NOWP_CRYPTO_DISABLE_INVOICE_CAP", "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    lst = max(0.0, float(list_price_usd))
    abs_extra = max(0.0, float(os.getenv("NOWP_CRYPTO_MAX_SURCHARGE_ABS_USD", "14")))
    pct = max(0.0, float(os.getenv("NOWP_CRYPTO_MAX_SURCHARGE_PCT", "35")))
    ceil_usd = round(lst + max(abs_extra, lst * pct / 100.0), 2)
    log.debug(
        "NOWPayments: invoice cap list=$%.4f ceiling=$%.4f (+max($%.4f flat, %.3f%%))",
        lst,
        ceil_usd,
        abs_extra,
        pct,
    )
    return ceil_usd


def _nowp_invoice_cap_blocked_message(list_price_usd: float, cap: float | None) -> str:
    cap_txt = "—"
    if cap is not None:
        cap_txt = f"${cap:.2f}"
    return (
        "NOWPayments вимагає мінімуму вище допустимого націнування до цінника в меню. "
        f"Тариф ${float(list_price_usd):.2f}; дозволена верхня межа інвойса — {cap_txt}. "
        "Спробуй оплату ⭐ або звернися до підтримки / адміна щодо криптомінімумів."
    )


def nowp_price_usd_above_crypto_minimum(price_usd: float) -> float:
    """
    Raise fiat list price slightly if NOWPayments rejects low crypto totals (AMOUNTMINIMALERROR).
    """
    floor = round(float(price_usd), 8)
    invoice_cap = nowp_crypto_invoice_ceiling_usd(floor)
    fallback_min = float(os.getenv("NOWPAYMENTS_MIN_PAY_CRYPTO_FALLBACK", "6"))
    margin = float(os.getenv("NOWPAYMENTS_MIN_PAY_MARGIN", "1.035"))
    # Extra absolute headroom **in pay_currency units** atop `min_crypto * margin`.
    abs_pad_crypto = max(0.0, float(os.getenv("NOWP_MIN_PAY_CRYPTO_ABS_PAD", "0.2")))
    # If NOWPayments/dashboard shows a larger floor than `/min-amount` returns for your merchant, raise it here (e.g. 13–20 for USDT TRC20).
    hard_floor = float(os.getenv("NOWP_MIN_PAY_CRYPTO_HARD_FLOOR", "0") or "0")
    step_usd = float(os.getenv("NOWPAYMENTS_MIN_TOPUP_STEP_USD", "0.1"))
    ceiling_usd = floor + float(os.getenv("NOWPAYMENTS_MIN_TOPUP_CEILING_USD", "120"))
    max_steps = max(50, int(os.getenv("NOWPAYMENTS_MIN_TOPUP_MAX_STEPS", "400")))

    parsed_min = nowp_minimum_pay_crypto()
    min_crypto = parsed_min if (parsed_min is not None and parsed_min > 0) else fallback_min
    if parsed_min is None or parsed_min <= 0:
        log.warning(
            "NOWPayments: min-amount API missing/bad — using NOWPAYMENTS_MIN_PAY_CRYPTO_FALLBACK=%s",
            fallback_min,
        )

    min_crypto_eff = float(min_crypto)
    if hard_floor > 0:
        if min_crypto_eff + 1e-9 < hard_floor:
            log.info(
                "NOWPayments: applying NOWP_MIN_PAY_CRYPTO_HARD_FLOOR=%.8f (was effective min_crypto≈ %.8f)",
                hard_floor,
                min_crypto_eff,
            )
        min_crypto_eff = max(min_crypto_eff, hard_floor)

    threshold = min_crypto_eff * margin + abs_pad_crypto

    usd_eff = floor
    for step_i in range(max_steps):
        if invoice_cap is not None and usd_eff > invoice_cap:
            raise RuntimeError(_nowp_invoice_cap_blocked_message(floor, invoice_cap))
        if usd_eff > ceiling_usd:
            raise RuntimeError(
                "NOWPayments: invoiced USD climbed above ceiling while sizing crypto minimum "
                f"({usd_eff:.4f} > {ceiling_usd:.4f}); check pay_currency or API."
            )

        estimated = nowp_estimate_pay_crypto_for_usd(usd_eff)
        if estimated is None:
            bump_fail = round(max(step_usd * 3.0, 0.18), 6)
            usd_eff = round(usd_eff + bump_fail, 6)
            if step_i + 1 == max_steps:
                raise RuntimeError("NOWPayments: estimate API unreachable; cannot size invoice.")
            continue

        if estimated >= threshold:
            cushion_usd = max(0.0, float(os.getenv("NOWP_ESTIMATE_CUSHION_USD", "0.55")))
            usd_final = round(usd_eff + cushion_usd, 10)
            if invoice_cap is not None and usd_final > invoice_cap + 1e-9:
                raise RuntimeError(_nowp_invoice_cap_blocked_message(floor, invoice_cap))
            if usd_eff > floor or cushion_usd > 0:
                log.info(
                    "NOWPayments: invoice sizing list=%.4f USD → fiat_try=%.6f + cushion=%.4f → final=%.8f USD "
                    "(min pay_crypto≈ %.8f %s, estimate_pay≈ %.10f)",
                    floor,
                    usd_eff,
                    cushion_usd,
                    usd_final,
                    min_crypto_eff,
                    NOWPAYMENTS_PAY_CURRENCY,
                    estimated,
                )
            return usd_final

        usd_eff = round(usd_eff + step_usd, 8)

    raise RuntimeError(
        f"NOWPayments: exceeded {max_steps} sizing steps starting from {floor:.8f} USD; "
        "try another NOWPAYMENTS_PAY_CURRENCY or increase ceiling env."
    )


def db_crypto_payment_upsert(
    payment_id: str,
    chat_id: int,
    plan: str,
    months: int,
    price_usd: float,
    pay_currency: str,
    pay_amount: float | None = None,
    pay_address: str | None = None,
    status: str = "waiting",
) -> None:
    with db_connect() as c:
        c.execute(
            "INSERT INTO crypto_payments(payment_id,chat_id,plan,months,price_usd,pay_currency,pay_amount,pay_address,status,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(payment_id) DO UPDATE SET "
            "pay_amount=excluded.pay_amount, pay_address=excluded.pay_address, status=excluded.status, updated_at=datetime('now')",
            (payment_id, chat_id, plan, months, price_usd, pay_currency, pay_amount, pay_address, status),
        )


def db_crypto_payment_get(payment_id: str) -> sqlite3.Row | None:
    with db_connect() as c:
        return c.execute(
            "SELECT * FROM crypto_payments WHERE payment_id=?",
            (payment_id,),
        ).fetchone()


def nowp_create_payment(chat_id: int, plan: str, months: int, price_usd: float) -> dict:
    """
    Create a NOWPayments invoice for USDT TRC20.
    Returns dict with payment_id, pay_address, pay_amount, invoice_url (if available).
    """
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY not set")
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL not set")

    requested_usd = float(price_usd)
    invoice_ceiling = nowp_crypto_invoice_ceiling_usd(requested_usd)

    initial_usd = round(nowp_price_usd_above_crypto_minimum(requested_usd), 10)
    price_try = float(initial_usd)
    if invoice_ceiling is not None and price_try > invoice_ceiling + 1e-9:
        raise RuntimeError(_nowp_invoice_cap_blocked_message(requested_usd, invoice_ceiling))

    pad_base = float(os.getenv("NOWP_AMOUNT_MINIMAL_ERR_PAD_USD", "0.35"))
    pad_ramp = float(os.getenv("NOWP_AMOUNT_MINIMAL_ERR_PAD_RAMP_USD", "0.05"))
    max_pay_retries = max(5, int(os.getenv("NOWP_AMOUNT_MINIMAL_ERR_MAX_RETRIES", "20")))
    last_detail = ""
    data: dict | None = None

    for pay_attempt in range(max_pay_retries):
        order_id = f"tg_{chat_id}_{plan}_{months}_{uuid.uuid4().hex[:10]}"
        invoiced_vs_list = abs(price_try - requested_usd) > 5e-3 or abs(price_try - initial_usd) > 5e-3
        payload = {
            "price_amount": float(price_try),
            "price_currency": "usd",
            "pay_currency": NOWPAYMENTS_PAY_CURRENCY,
            "order_id": order_id,
            "order_description": (
                f"Telegram subscription: {plan} x{months}mo (chat {chat_id}); "
                f"list ${requested_usd:.2f}; invoiced ${price_try:.2f} USD equiv"
                if invoiced_vs_list
                else f"Telegram subscription: {plan} x{months}mo (chat {chat_id})"
            ),
            "ipn_callback_url": f"{PUBLIC_BASE_URL.rstrip('/')}/nowpayments",
        }
        fixed_on = (
            os.getenv("NOWP_PAYMENTS_FIXED_RATE", "1").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if fixed_on:
            payload["fixed_rate"] = True

        log.info(
            "NOWPayments: POST /payment attempt %s/%s chat=%s plan=%s/%smo listed_usd=%.4f try_usd=%.6f pay=%s fixed_rate=%s",
            pay_attempt + 1,
            max_pay_retries,
            chat_id,
            plan,
            months,
            requested_usd,
            price_try,
            NOWPAYMENTS_PAY_CURRENCY,
            fixed_on,
        )

        r = requests.post(f"{NOWPAYMENTS_BASE}/payment", headers=_nowp_headers(), json=payload, timeout=12)
        if r.ok:
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError("NOWPayments: payment response was not JSON")
            break

        last_detail = _nowp_response_error_detail(r)
        log.warning(
            "NOWPayments POST /payment attempt %s failed: status=%s body=%s",
            pay_attempt + 1,
            r.status_code,
            last_detail,
        )
        if (
            pay_attempt + 1 < max_pay_retries
            and ("AMOUNTMINIMAL" in last_detail.upper() or " LESS THAN MINIMAL" in last_detail.upper())
        ):
            bump = pad_base + pad_ramp * float(pay_attempt)
            nxt_try = round(float(price_try) + bump, 10)
            if invoice_ceiling is not None and nxt_try > invoice_ceiling + 1e-9:
                raise RuntimeError(_nowp_invoice_cap_blocked_message(requested_usd, invoice_ceiling))
            price_try = nxt_try
            log.warning(
                "NOWPayments: AMOUNTMINIMALERROR → +%.4f USD retry toward %.8f USD",
                bump,
                price_try,
            )
            continue

        raise RuntimeError(f"NOWPayments error ({r.status_code}): {last_detail}")

    if data is None:
        raise RuntimeError(last_detail or "NOWPayments payment create failed unexpectedly")

    price_amount = round(float(price_try), 10)
    payment_id = str(data.get("payment_id") or "")
    if not payment_id:
        raise RuntimeError(f"NOWPayments create payment failed: {data}")

    pay_address = data.get("pay_address")
    pay_amount = data.get("pay_amount")
    invoice_url = data.get("invoice_url") or data.get("payment_url")

    db_crypto_payment_upsert(
        payment_id=payment_id,
        chat_id=chat_id,
        plan=plan,
        months=months,
        price_usd=float(price_amount),
        pay_currency=NOWPAYMENTS_PAY_CURRENCY,
        pay_amount=float(pay_amount) if pay_amount else None,
        pay_address=str(pay_address) if pay_address else None,
        status=str(data.get("payment_status") or "waiting"),
    )
    return {
        "payment_id": payment_id,
        "pay_address": pay_address,
        "pay_amount": pay_amount,
        "pay_currency": NOWPAYMENTS_PAY_CURRENCY,
        "invoice_url": invoice_url,
        "raw": data,
        "price_usd_requested": requested_usd,
        "price_usd_invoiced": float(price_amount),
    }


def nowp_get_payment(payment_id: str) -> dict:
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY not set")
    r = requests.get(f"{NOWPAYMENTS_BASE}/payment/{payment_id}", headers=_nowp_headers(), timeout=12)
    if not r.ok:
        detail = _nowp_response_error_detail(r)
        log.warning("NOWPayments GET /payment/%s failed: status=%s body=%s", payment_id, r.status_code, detail)
        raise RuntimeError(f"NOWPayments error ({r.status_code}): {detail}")
    return r.json()


def _nowp_is_paid(status: str) -> bool:
    s = (status or "").lower()
    return s in ("finished", "confirmed", "sending")


def _nowp_is_failed(status: str) -> bool:
    s = (status or "").lower()
    return s in ("failed", "expired", "refunded")


def db_has_charge_id(charge_id: str) -> bool:
    """True if a payment with this charge_id was already applied."""
    with db_connect() as c:
        row = c.execute(
            "SELECT 1 FROM payments WHERE telegram_charge_id=? LIMIT 1",
            (charge_id,),
        ).fetchone()
    return row is not None


def db_stats() -> dict:
    with db_connect() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        trial   = c.execute("SELECT COUNT(*) FROM users WHERE plan='trial'").fetchone()[0]
        basic   = c.execute("SELECT COUNT(*) FROM users WHERE plan='basic'").fetchone()[0]
        pro     = c.execute("SELECT COUNT(*) FROM users WHERE plan='pro'").fetchone()[0]
        diamond = c.execute("SELECT COUNT(*) FROM users WHERE plan='diamond'").fetchone()[0]
        exp     = c.execute("SELECT COUNT(*) FROM users WHERE plan='expired'").fetchone()[0]
        stars   = c.execute("SELECT SUM(stars) FROM payments").fetchone()[0] or 0
        posts = c.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0]
    return dict(total=total, trial=trial, basic=basic, pro=pro, diamond=diamond,
                expired=exp, total_stars=stars, posts=posts)


def db_save_post(pair: str, post_type: str, score: int,
                 sentiment: str, price: float, message_id: int) -> None:
    with db_connect() as c:
        c.execute(
            "INSERT INTO channel_posts(pair,post_type,score,sentiment,price,message_id) "
            "VALUES(?,?,?,?,?,?)",
            (pair, post_type, score, sentiment, price, message_id),
        )


def db_save_trade(chat_id: int, pair: str, entry_price: float | None,
                  waiting_price: float | None, sl_warning: bool,
                  last_sig_time: float, last_sig_score: int) -> None:
    with db_connect() as c:
        c.execute(
            "INSERT INTO active_trades VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,pair) DO UPDATE SET "
            "entry_price=excluded.entry_price, waiting_price=excluded.waiting_price, "
            "sl_warning_sent=excluded.sl_warning_sent, "
            "last_signal_time=excluded.last_signal_time, "
            "last_signal_score=excluded.last_signal_score",
            (chat_id, pair, entry_price, waiting_price,
             int(sl_warning), last_sig_time, last_sig_score),
        )


def db_load_trades(chat_id: int) -> dict:
    with db_connect() as c:
        rows = c.execute(
            "SELECT * FROM active_trades WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {r["pair"]: r for r in rows}


def db_delete_trade(chat_id: int, pair: str) -> None:
    with db_connect() as c:
        c.execute("DELETE FROM active_trades WHERE chat_id=? AND pair=?", (chat_id, pair))


# ── Referral system ─────────────────────────────────────────────

REFERRAL_BONUS_DAYS = 3   # days added to referrer per successful referral


def db_save_utm(cid: int, source: str) -> None:
    """Save UTM/traffic source for a new user."""
    with db_connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO utm_sources(chat_id, source) VALUES(?,?)",
            (cid, source),
        )


def db_register_referral(referrer_id: int, referred_id: int, source: str = "ref") -> bool:
    """Register a referral. Returns True if new (not already registered)."""
    try:
        with db_connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO referrals(referrer_id, referred_id, source) VALUES(?,?,?)",
                (referrer_id, referred_id, source),
            )
            return c.execute(
                "SELECT changes()"
            ).fetchone()[0] > 0
    except Exception as e:
        log.warning("db_register_referral: %s", e)
        return False


def db_give_referral_bonus(referrer_id: int, referred_id: int) -> int:
    """
    Give bonus days to referrer when referred user activates (starts bot).
    Returns number of bonus days given (0 if already given).
    """
    with db_connect() as c:
        row = c.execute(
            "SELECT id, bonus_given FROM referrals "
            "WHERE referrer_id=? AND referred_id=?",
            (referrer_id, referred_id),
        ).fetchone()
        if not row or row["bonus_given"]:
            return 0

        # Extend referrer trial or subscription
        u = c.execute("SELECT plan, trial_ends, sub_expires FROM users WHERE chat_id=?",
                      (referrer_id,)).fetchone()
        if not u:
            return 0

        today = datetime.now(UTC).date()
        if u["plan"] in ("basic", "pro", "diamond") and u["sub_expires"]:
            base = max(datetime.strptime(u["sub_expires"], "%Y-%m-%d").date(), today)
            new_date = base + timedelta(days=REFERRAL_BONUS_DAYS)
            c.execute("UPDATE users SET sub_expires=? WHERE chat_id=?",
                      (new_date.strftime("%Y-%m-%d"), referrer_id))
        elif u["plan"] == "trial" and u["trial_ends"]:
            base = max(datetime.strptime(u["trial_ends"], "%Y-%m-%d").date(), today)
            new_date = base + timedelta(days=REFERRAL_BONUS_DAYS)
            c.execute("UPDATE users SET trial_ends=? WHERE chat_id=?",
                      (new_date.strftime("%Y-%m-%d"), referrer_id))
        else:
            return 0

        c.execute("UPDATE referrals SET bonus_given=1 WHERE id=?", (row["id"],))
        return REFERRAL_BONUS_DAYS


def db_referral_stats(cid: int) -> dict:
    """Get referral stats for a user."""
    with db_connect() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (cid,)
        ).fetchone()[0]
        bonused = c.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND bonus_given=1", (cid,)
        ).fetchone()[0]
        pending = total - bonused
    return {"total": total, "bonused": bonused, "pending": pending,
            "days_earned": bonused * REFERRAL_BONUS_DAYS}


def db_utm_stats() -> dict:
    """Admin: traffic source breakdown."""
    with db_connect() as c:
        rows = c.execute(
            "SELECT source, COUNT(*) as cnt FROM utm_sources GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
    return {r["source"]: r["cnt"] for r in rows}


# ═══════════════════════════════════════════════════════════════════
#  In-memory state
# ═══════════════════════════════════════════════════════════════════

class PairState:
    __slots__ = ("entry_price", "running", "waiting_entry_price",
                 "sl_warning_sent", "last_signal_time", "last_signal_score")

    def __init__(self) -> None:
        self.entry_price:         float | None = None
        self.running:             bool         = False
        self.waiting_entry_price: float | None = None
        self.sl_warning_sent:     bool         = False
        self.last_signal_time:    float        = 0.0
        self.last_signal_score:   int          = 0

    @property
    def has_trade(self) -> bool:
        return self.entry_price is not None and self.running

    @property
    def is_waiting(self) -> bool:
        return self.waiting_entry_price is not None and not self.running

    def persist(self, chat_id: int, pair: str) -> None:
        db_save_trade(
            chat_id, pair,
            self.entry_price if self.running else None,
            self.waiting_entry_price,
            self.sl_warning_sent,
            self.last_signal_time,
            self.last_signal_score,
        )

    def reset(self, chat_id: int, pair: str) -> None:
        self.entry_price = None
        self.running = False
        self.waiting_entry_price = None
        self.sl_warning_sent = False
        db_delete_trade(chat_id, pair)


class UserState:
    def __init__(self, cid: int) -> None:
        self.chat_id:          int                  = cid
        self.selected_pair:    str                  = DEFAULT_PAIR
        self.pairs:            dict[str, PairState] = {p: PairState() for p in PAIRS}
        self.pending_analysis: dict | None          = None

    def ps(self) -> PairState:
        return self.pairs[self.selected_pair]

    def restore_from_db(self) -> None:
        for pair, row in db_load_trades(self.chat_id).items():
            if pair not in self.pairs:
                continue
            ps = self.pairs[pair]
            if row["entry_price"] is not None:
                ps.entry_price = row["entry_price"]
                ps.running = True
            if row["waiting_price"] is not None:
                ps.waiting_entry_price = row["waiting_price"]
            ps.sl_warning_sent   = bool(row["sl_warning_sent"])
            ps.last_signal_time  = row["last_signal_time"]
            ps.last_signal_score = row["last_signal_score"]


USERS: dict[int, UserState] = {}
_prices:      dict[str, float | None] = {p: None for p in PAIRS}
_prev_prices: dict[str, float | None] = {p: None for p in PAIRS}
_last_channel_post_hour: int = -1
_last_article_hour:      int = -1
_article_index:          int = 0


def get_user(cid: int) -> UserState:
    if cid not in USERS:
        u = UserState(cid)
        u.restore_from_db()
        USERS[cid] = u
    return USERS[cid]


# ═══════════════════════════════════════════════════════════════════
#  Market data
# ═══════════════════════════════════════════════════════════════════


_BINANCE_SYMBOLS = {
    'BTCUSD':  'BTCUSDT',
    'ETHUSD':  'ETHUSDT',
    'SOLUSD':  'SOLUSDT',
    'XRPUSD':  'XRPUSDT',
    'BNBUSD':  'BNBUSDT',
    'ADAUSD':  'ADAUSDT',
}
PRICE_RANGES = {
    "XAUUSD": (500,    15_000),
    "XAGUSD": (10,     500),
    "BTCUSD": (1_000,  500_000),
    "ETHUSD": (50,     50_000),
    "SOLUSD": (1,      5_000),
    "XRPUSD": (0.01,   100),
    "BNBUSD": (10,     5_000),
    "ADAUSD": (0.01,   50),
}


def _valid_price(price: float, pair: str) -> bool:
    lo, hi = PRICE_RANGES.get(pair, (0, 1e9))
    return lo < price < hi


def get_price(pair: str) -> float | None:
    """
    Fetch spot price with in-memory cache (45s TTL).
    Sources: Yahoo (GC=F/BTC-USD/ETH-USD) → Binance (crypto) → stale cache
    Note: Stooq removed — blocked on Hetzner servers.
    """
    cached = _price_cache.get(pair)
    if cached and (time.time() - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]

    if pair in ("XAUUSD", "XAGUSD"):
        decimals = 2
    elif pair in ("BTCUSD", "ETHUSD", "BNBUSD", "SOLUSD"):
        decimals = 0
    else:
        decimals = 4  # XRP, ADA etc

    def _save(p: float) -> float:
        rounded = round(p, decimals)
        _price_cache[pair] = (rounded, time.time())
        return rounded

    # ── Swissquote — real spot price for XAU/XAG, free, no key ──
    if pair in ("XAUUSD", "XAGUSD"):
        sq_symbol = "XAU" if pair == "XAUUSD" else "XAG"
        try:
            r = requests.get(
                f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{sq_symbol}/USD",
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            prices = data[0]["spreadProfilePrices"]
            # use "prime" spread profile mid price
            for sp in prices:
                if sp.get("spreadProfile") == "prime":
                    p = round((float(sp["bid"]) + float(sp["ask"])) / 2, 2)
                    if _valid_price(p, pair):
                        log.debug("Price %s = %s (Swissquote spot)", pair, p)
                        return _save(p)
        except Exception as e:
            log.debug("Swissquote (%s): %s", pair, e)

    # ── Gold API (goldapi.io) — spot price for XAU/XAG ──
    if pair in ("XAUUSD", "XAGUSD") and GOLD_API_KEY:
        symbol = "XAU" if pair == "XAUUSD" else "XAG"
        try:
            r = requests.get(
                f"https://www.goldapi.io/api/{symbol}/USD",
                headers={"x-access-token": GOLD_API_KEY, "Content-Type": "application/json"},
                timeout=6,
            )
            r.raise_for_status()
            p = float(r.json().get("price", 0))
            if p and _valid_price(p, pair):
                log.debug("Price %s = %s (GoldAPI spot)", pair, p)
                return _save(p)
        except Exception as e:
            log.debug("GoldAPI (%s): %s", pair, e)

    # ── metals.live — free spot price for XAU/XAG ──
    if pair in ("XAUUSD", "XAGUSD"):
        symbol = "gold" if pair == "XAUUSD" else "silver"
        try:
            r = requests.get("https://metals.live/api/spot", timeout=6)
            r.raise_for_status()
            for item in r.json():
                if item.get("metal", "").lower() == symbol:
                    p = float(item.get("price", 0))
                    if p and _valid_price(p, pair):
                        log.debug("Price %s = %s (metals.live spot)", pair, p)
                        return _save(p)
        except Exception as e:
            log.debug("metals.live (%s): %s", pair, e)

    # Binance — real-time for crypto
    if pair in _BINANCE_SYMBOLS:
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={_BINANCE_SYMBOLS[pair]}",
                timeout=5)
            p = float(r.json()["price"])
            if p and _valid_price(p, pair):
                return _save(p)
        except Exception as e:
            log.debug("Binance (%s): %s", pair, e)

    # Yahoo Finance — GC=F for gold, SI=F for silver, direct tickers for crypto
    yahoo_tickers = {
        "XAUUSD": ["GC%3DF"],       # Gold futures
        "XAGUSD": ["SI%3DF"],       # Silver futures
        "BTCUSD": ["BTC-USD"],
        "ETHUSD": ["ETH-USD"],
        "SOLUSD": ["SOL-USD"],
        "XRPUSD": ["XRP-USD"],
        "BNBUSD": ["BNB-USD"],
        "ADAUSD": ["ADA-USD"],
    }
    for ticker in yahoo_tickers.get(pair, []):
        for base in ("https://query1.finance.yahoo.com",
                     "https://query2.finance.yahoo.com"):
            try:
                r = requests.get(
                    f"{base}/v8/finance/chart/{ticker}",
                    headers={"User-Agent": "Mozilla/5.0",
                             "Accept": "application/json"},
                    timeout=8,
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                p = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
                if p and _valid_price(float(p), pair):
                    log.debug("Price %s = %s (Yahoo %s)", pair, p, ticker)
                    return _save(float(p))
            except Exception as e:
                log.debug("Yahoo %s/%s: %s", pair, ticker, e)

    # Binance — free, no auth, reliable for crypto
    binance_sym = {
        "BTCUSD": "BTCUSDT",
        "ETHUSD": "ETHUSDT",
        "SOLUSD": "SOLUSDT",
        "XRPUSD": "XRPUSDT",
        "BNBUSD": "BNBUSDT",
        "ADAUSD": "ADAUSDT",
    }
    if pair in binance_sym:
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/price"
                f"?symbol={binance_sym[pair]}",
                timeout=6,
            )
            r.raise_for_status()
            p = float(r.json()["price"])
            if _valid_price(p, pair):
                return _save(p)
        except Exception as e:
            log.warning("Binance (%s): %s", pair, e)

    # Stale cache — better than nothing
    if cached:
        age = int(time.time() - cached[1])
        log.warning("All price sources failed for %s — stale cache (%ds old)", pair, age)
        return cached[0]

    log.error("Cannot get price for %s", pair)
    return None


def get_news(pair: str) -> str:
    """
    Fetch news with 5-minute cache to stay within NewsAPI 100 req/day free limit.
    Priority: RSS feeds (free/unlimited) → NewsAPI → stale cache
    """
    cached = _news_cache.get(pair)
    if cached and (time.time() - cached[1]) < _NEWS_CACHE_TTL:
        return cached[0]

    def _save_news(text: str) -> str:
        _news_cache[pair] = (text, time.time())
        return text

    # 1. RSS — free, unlimited, no API key needed
    try:
        rss_items = get_news_rss()
        if rss_items:
            return _save_news(" | ".join(i["title"] for i in rss_items[:5]))
    except Exception:
        pass

    # 2. NewsAPI fallback (only when RSS fails)
    q = PAIRS[pair]["news_q"]
    try:
        r = requests.get(
            f"https://newsapi.org/v2/everything"
            f"?q={q.replace(' ', '%20')}&sortBy=publishedAt"
            f"&pageSize=5&apiKey={NEWS_API}",
            timeout=6,
        )
        r.raise_for_status()
        result = " | ".join(a.get("title", "") for a in r.json().get("articles", [])[:5])
        return _save_news(result)
    except Exception as e:
        log.warning("NewsAPI (%s): %s", pair, e)

    # 3. Stale cache
    if cached:
        return cached[0]

    return ""


def get_technicals(pair: str) -> dict:
    """
    Fetch technical indicators via yfinance.
    Fixes yfinance >=0.2.x Series bug by using .squeeze() and .iloc[-1].
    """
    def _fetch():
        import yfinance as yf
        import pandas as pd

        ticker = PAIRS[pair]["yahoo"]
        df = yf.download(ticker, period="5d", interval="15m",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            raise ValueError(f"Not enough data: {len(df)} rows")

        # Fix yfinance multi-level columns (happens with newer versions)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Flatten any remaining Series-in-Series issues
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].squeeze(), errors="coerce")
        df = df.dropna(subset=["Close"])

        closes = df["Close"].values.astype(float)
        highs  = df["High"].values.astype(float)
        lows   = df["Low"].values.astype(float)

        # RSI manually (no pandas-ta dependency issues)
        def _rsi(arr, period=14):
            delta = pd.Series(arr).diff()
            gain  = delta.clip(lower=0).rolling(period).mean()
            loss  = (-delta.clip(upper=0)).rolling(period).mean() + 1e-9
            rs    = gain / loss
            return float((100 - 100 / (1 + rs)).iloc[-1])

        # EMA
        def _ema(arr, span):
            return float(pd.Series(arr).ewm(span=span, adjust=False).mean().iloc[-1])

        rsi   = _rsi(closes)
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        price = float(closes[-1])

        # MACD
        ema12  = pd.Series(closes).ewm(span=12, adjust=False).mean()
        ema26  = pd.Series(closes).ewm(span=26, adjust=False).mean()
        macd   = float((ema12 - ema26).iloc[-1])
        signal = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])

        # Support / Resistance from recent swing highs/lows
        recent_h = highs[-30:] if len(highs) >= 30 else highs
        recent_l = lows[-30:]  if len(lows)  >= 30 else lows
        support  = round(float(pd.Series(recent_l).nsmallest(3).mean()), 2)
        resist   = round(float(pd.Series(recent_h).nlargest(3).mean()),  2)

        return {
            "ok":   True,
            "rsi":  round(rsi, 1),
            "rsi_zone": ("overbought" if rsi > 70 else
                         "oversold"   if rsi < 30 else "neutral"),
            "macd_cross":     "bullish" if macd > signal else "bearish",
            "ema20":          round(ema20, 2),
            "ema50":          round(ema50, 2),
            "ema_trend":      "up" if ema20 > ema50 else "down",
            "price_vs_ema20": "above" if price > ema20 else "below",
            "support1":       support,
            "resist1":        resist,
            "price":          round(price, 2),
        }

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_fetch).result(timeout=20)
    except concurrent.futures.TimeoutError:
        log.warning("Technicals timeout (%s)", pair)
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log.warning("Technicals (%s): %s", pair, e)
        return {"ok": False, "error": str(e)[:80]}


# ═══════════════════════════════════════════════════════════════════
#  OpenRouter — Groq fallback (multi-key round-robin + 429 failover)
# ═══════════════════════════════════════════════════════════════════

def _openrouter_headers_for(api_key: str) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL.strip():
        h["HTTP-Referer"] = OPENROUTER_SITE_URL.strip()
    if OPENROUTER_APP_TITLE.strip():
        h["X-Title"] = OPENROUTER_APP_TITLE.strip()
    return h


def _next_openrouter_rr_start() -> int:
    """Rotate which key is tried first on each request (spread load across accounts)."""
    n = len(_OPENROUTER_KEYS)
    if n <= 1:
        return 0
    global _openrouter_rr_i
    with _openrouter_rr_lock:
        idx = _openrouter_rr_i % n
        _openrouter_rr_i += 1
        return idx


def _openrouter_failover_eligible(http_status: int, err_msg: str) -> bool:
    """Whether to try the next API key (quota / transient), not e.g. invalid JSON body."""
    if http_status == 400:
        return False
    if http_status in (401, 402, 408, 429, 502, 503, 529):
        return True
    m = (err_msg or "").lower()
    if "rate" in m and "limit" in m:
        return True
    if "quota" in m or "exceed" in m:
        return True
    if "insufficient" in m:
        return True
    return False


def _parse_openrouter_message_json(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if content is None:
        raise RuntimeError("OpenRouter empty response")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = "".join(parts)
    text = str(content).strip()
    if not text:
        raise RuntimeError("OpenRouter empty response")
    return text


def _openrouter_post_once(api_key: str, payload: dict) -> tuple[bool, str, int, str]:
    """
    Single OpenRouter HTTP call.
    Returns (success, text_if_ok, http_status, error_message_on_failure).
    """
    r = requests.post(
        OPENROUTER_API_URL,
        headers=_openrouter_headers_for(api_key),
        json=payload,
        timeout=120,
    )
    try:
        data = r.json()
    except Exception:
        data = {}
    sc = r.status_code
    if sc == 200:
        try:
            return True, _parse_openrouter_message_json(data), 200, ""
        except Exception as e:
            return False, "", sc, str(e)
    err_o = data.get("error")
    if isinstance(err_o, dict):
        msg = (err_o.get("message") or str(err_o))[:800]
    elif isinstance(err_o, str):
        msg = err_o[:800]
    else:
        msg = (r.text or "")[:400]
    return False, "", sc, msg or f"HTTP {sc}"


def _openrouter_vision_model() -> str:
    v = (OPENROUTER_VISION_MODEL or "").strip()
    return v if v else OPENROUTER_MODEL


def _openrouter_chat(
    messages: list,
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.35,
    response_format: dict | None = None,
) -> str:
    if not _OPENROUTER_KEYS:
        raise RuntimeError(
            "OpenRouter API keys not configured — set OPENROUTER_API_KEY and/or OPENROUTER_API_KEY_2",
        )
    use_model = (model or OPENROUTER_MODEL).strip()
    payload: dict = {
        "model": use_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    n = len(_OPENROUTER_KEYS)
    start = _next_openrouter_rr_start()
    last_err = ""
    last_sc = 0

    for attempt in range(n):
        k_ix = (start + attempt) % n
        api_key = _OPENROUTER_KEYS[k_ix]
        ok, text, sc, err = _openrouter_post_once(api_key, payload)
        if ok:
            if attempt > 0:
                log.info("OpenRouter: OK using key #%s after failover/rotation", k_ix)
            return text
        last_err, last_sc = err, sc
        if not _openrouter_failover_eligible(sc, err):
            raise RuntimeError(err or f"OpenRouter HTTP {sc}")
        if attempt < n - 1:
            log.warning(
                "OpenRouter key #%s HTTP %s — %s; trying next key",
                k_ix, sc, (err or "")[:160],
            )

    raise RuntimeError(last_err or f"OpenRouter HTTP {last_sc} (all keys exhausted)")


def _openrouter_text(prompt: str, max_tokens: int = 500) -> str:
    """Generate plain text via OpenRouter. Used as Groq fallback for articles."""
    return _openrouter_chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.45,
    )


def _openrouter_json_analysis(prompt: str) -> str:
    """Ask OpenRouter for a JSON trading signal. Used as Groq fallback."""
    return _openrouter_chat(
        [{"role": "user", "content": prompt + "\n\nReply with ONLY valid JSON, no markdown code fences."}],
        max_tokens=400,
        temperature=0.25,
    )


def _gemini_text(prompt: str, max_tokens: int = 500) -> str:
    """Generate text via Gemini. Used as Groq fallback after OpenRouter."""
    import google.genai as genai
    import google.genai.types as gtypes

    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=gtypes.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return response.text


def _gemini_json_analysis(prompt: str) -> str:
    """Ask Gemini for a JSON trading signal. Groq fallback after OpenRouter."""
    import google.genai as genai
    import google.genai.types as gtypes

    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            max_output_tokens=300,
            response_mime_type="application/json",
        ),
    )
    return response.text


def _groq_fallback_json_analysis(prompt: str) -> str:
    """After Groq 429: try OpenRouter, then Gemini."""
    if _openrouter_configured():
        try:
            return _openrouter_json_analysis(prompt)
        except Exception as e:
            log.warning("OpenRouter JSON fallback failed: %s", e)
    if GEMINI_KEY:
        return _gemini_json_analysis(prompt)
    raise RuntimeError(
        "Groq rate-limited: set OpenRouter (`OPENROUTER_API_KEY` / `OPENROUTER_API_KEY_2`) and/or `GEMINI_KEY` for fallback AI"
    )


def _groq_fallback_article_text(prompt: str, max_tokens: int = 500) -> str:
    """After Groq 429 on article: try OpenRouter, then Gemini."""
    if _openrouter_configured():
        try:
            return _openrouter_text(prompt, max_tokens=max_tokens)
        except Exception as e:
            log.warning("OpenRouter article fallback failed: %s", e)
    if GEMINI_KEY:
        return _gemini_text(prompt, max_tokens=max_tokens)
    raise RuntimeError(
        "Groq rate-limited: set OpenRouter (`OPENROUTER_API_KEY` / `OPENROUTER_API_KEY_2`) and/or `GEMINI_KEY` for fallback AI"
    )


def _make_sl_tp(price: float, direction: str, sl_pct: float, tp_pct: float) -> tuple[float, float]:
    """Calculate SL and TP correctly based on trade direction."""
    if direction == "SELL":
        sl = round(price * (1 + sl_pct / 100), 2)   # SL above entry for SELL
        tp = round(price * (1 - tp_pct / 100), 2)   # TP below entry for SELL
    else:  # BUY
        sl = round(price * (1 - sl_pct / 100), 2)   # SL below entry for BUY
        tp = round(price * (1 + tp_pct / 100), 2)   # TP above entry for BUY
    return sl, tp


# ═══════════════════════════════════════════════════════════════════
#  Groq analysis  (model chosen per call: NEWS vs SIGNALS; then OpenRouter / Gemini on 429)
# ═══════════════════════════════════════════════════════════════════

def _normalize_confidence(raw_conf) -> int:
    """Normalize confidence to 0-100 integer. Handles 0.85 → 85 and 85 → 85."""
    try:
        v = float(raw_conf)
        if v <= 1.0:          # Groq returned 0.0–1.0 fraction
            v = v * 100
        return max(0, min(100, int(round(v))))
    except (TypeError, ValueError):
        return 35


def _groq_client():
    from groq import Groq
    return Groq(api_key=GROQ_KEY)


def groq_analysis(news_text: str, price: float, tech: dict,
                  trend: str, vol: str, pair: str, groq_model: str) -> dict:
    cfg     = PAIRS[pair]
    sl_hint = cfg["sl_pct"]
    tp_hint = cfg["tp_pct"]

    # Determine likely direction early for correct fallback SL/TP
    _sent_guess = "neutral"
    if tech.get("ok"):
        bull = sum([tech.get("macd_cross") == "bullish",
                    tech.get("ema_trend")  == "up",
                    tech.get("price_vs_ema20") == "above"])
        _sent_guess = "bullish" if bull >= 2 else ("bearish" if bull == 0 else "neutral")
    _dir_guess = "SELL" if _sent_guess == "bearish" or trend == "down" else "BUY"
    _sl_fb, _tp_fb = _make_sl_tp(price, _dir_guess, sl_hint, tp_hint)

    fallback = {
        "sentiment": "neutral", "confidence": 35, "risk_level": "medium",
        "recommendation": "wait",
        "optimal_entry": round(price * (0.998 if _dir_guess == "BUY" else 1.002), 2),
        "stop_loss":      _sl_fb,
        "take_profit":    _tp_fb,
        "risk_reward":    f"1:{tp_hint / sl_hint:.1f}",
        "entry_reason": "fallback", "main_driver": "fallback",
    }
    try:
        tb = "unavailable"
        if tech.get("ok"):
            tb = (f"RSI={tech['rsi']}({tech['rsi_zone']}), MACD={tech['macd_cross']}, "
                  f"EMA20={tech['ema20']}, EMA50={tech['ema50']}, "
                  f"Support={tech['support1']}, Resistance={tech['resist1']}")
        analysis_prompt = (
                f"You are a senior {cfg['name']} trader.\n"
                f"Current price: {price}, Trend: {trend}, Volatility: {vol}\n"
                f"Technicals: {tb}\nNews: {news_text[:300]}\n\n"
                "Reply ONLY with valid JSON. Rules:\n"
                "- sentiment: 'bullish' or 'bearish' or 'neutral'\n"
                "- confidence: integer 0-100 (NOT a decimal like 0.85)\n"
                "- risk_level: 'low' or 'medium' or 'high' or 'extreme'\n"
                "- recommendation: 'enter_now' or 'wait_for_pullback' or 'wait' or 'avoid'\n"
                "- If sentiment is bearish: stop_loss MUST be ABOVE entry, take_profit MUST be BELOW entry\n"
                "- If sentiment is bullish: stop_loss MUST be BELOW entry, take_profit MUST be ABOVE entry\n"
                "Keys: sentiment, confidence, risk_level, recommendation, "
                "optimal_entry, stop_loss, take_profit, risk_reward, entry_reason, main_driver"
        )
        try:
            raw = _groq_client().chat.completions.create(
                model=groq_model, timeout=GROQ_TIMEOUT,
                messages=[{"role": "user", "content": analysis_prompt}],
                temperature=0.3, max_tokens=280,
            ).choices[0].message.content
        except Exception as groq_err:
            if "429" in str(groq_err) or "rate_limit" in str(groq_err).lower():
                log.info("Groq rate limit — trying OpenRouter then Gemini for analysis")
                raw = _groq_fallback_json_analysis(analysis_prompt)
            else:
                raise
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            # Normalize confidence (fix 0.0–1.0 fraction from Groq)
            parsed["confidence"] = _normalize_confidence(parsed.get("confidence", 35))
            # Validate and fix SL/TP direction
            entry = float(parsed.get("optimal_entry") or price)
            sl    = float(parsed.get("stop_loss") or 0)
            tp    = float(parsed.get("take_profit") or 0)
            sent  = (parsed.get("sentiment") or "neutral").lower()
            if sl > 0 and tp > 0:
                if sent == "bearish" and (sl < entry or tp > entry):
                    log.warning("Groq SL/TP direction mismatch for SELL — fixing")
                    sl, tp = _make_sl_tp(entry, "SELL", sl_hint, tp_hint)
                    parsed["stop_loss"]   = sl
                    parsed["take_profit"] = tp
                elif sent == "bullish" and (sl > entry or tp < entry):
                    log.warning("Groq SL/TP direction mismatch for BUY — fixing")
                    sl, tp = _make_sl_tp(entry, "BUY", sl_hint, tp_hint)
                    parsed["stop_loss"]   = sl
                    parsed["take_profit"] = tp
            return {**fallback, **parsed}
    except Exception as e:
        log.warning("AI analysis failed (Groq+OpenRouter+Gemini): %s", e)
    return fallback


def _check_econ_calendar() -> dict:
    today = date.today().isoformat()
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=6
        )
        r.raise_for_status()
        dangerous = [
            e["title"] for e in r.json()
            if e.get("date", "").startswith(today)
            and e.get("impact") == "High"
            and e.get("country") in ("USD", "US")
        ]
        return {"has_danger": bool(dangerous), "events": dangerous[:3]}
    except Exception:
        return {"has_danger": False, "events": []}


_SW = {
    "trend_up":   20, "trend_flat":  10, "trend_down": -10,  # bearish trend penalises
    "vol_normal": 10, "vol_high":     5, "vol_chaos":   0,
    "rsi_neutral": 7, "macd_bull":    7, "ema_up":      4, "above_ema": 2,
    "safe":       15, "risk_lbl":     8, "conf_bonus":  5,
    "no_econ":    10, "enter":       10, "wait":        5, "danger_cap": 30,
}


def _calc_score(tech: dict, ai: dict, econ: dict, trend: str, vol: str) -> int:
    lbl = _sentiment_label(ai)
    s   = 0

    # Trend — score is symmetric for BUY vs SELL
    sent = (ai.get("sentiment") or "neutral").lower()
    is_bear = sent == "bearish"
    if trend == "flat":
        s += _SW["trend_flat"]
    else:
        aligned = (trend == "up" and not is_bear) or (trend == "down" and is_bear)
        s += _SW["trend_up"] if aligned else _SW["trend_down"]

    # Volatility
    if vol == "normal": s += _SW["vol_normal"]
    elif vol == "high": s += _SW["vol_high"]
    # chaos adds nothing

    # Technicals — only add when direction aligns with sentiment
    if tech.get("ok"):
        if sent != "bearish":   # for BUY/neutral signals
            s += min(
                _SW["rsi_neutral"] * (tech["rsi_zone"]      == "neutral")
                + _SW["macd_bull"] * (tech["macd_cross"]     == "bullish")
                + _SW["ema_up"]    * (tech.get("ema_trend")  == "up")
                + _SW["above_ema"] * (tech["price_vs_ema20"] == "above"),
                20,
            )
        else:                   # for SELL signals, bearish tech adds points
            s += min(
                _SW["rsi_neutral"] * (tech["rsi_zone"]       == "neutral")
                + _SW["macd_bull"] * (tech["macd_cross"]      == "bearish")
                + _SW["ema_up"]    * (tech.get("ema_trend")   == "down")
                + _SW["above_ema"] * (tech["price_vs_ema20"]  == "below"),
                20,
            )

    # AI sentiment label
    s += _SW["safe"] if lbl == "SAFE" else (_SW["risk_lbl"] if lbl == "RISK" else 0)

    # Confidence modifier
    conf = ai.get("confidence", 50)
    if lbl == "SAFE"   and conf >= 70: s += _SW["conf_bonus"]
    if lbl == "DANGER" and conf >= 70: s -= _SW["conf_bonus"]

    # Economic calendar
    s += _SW["no_econ"] if not econ.get("has_danger") else 0

    # AI recommendation
    rec = ai.get("recommendation", "")
    s += _SW["enter"] if rec == "enter_now" else (_SW["wait"] if rec == "wait_for_pullback" else 0)

    # Hard cap on danger signals
    if lbl == "DANGER" or econ.get("has_danger"):
        s = min(s, _SW["danger_cap"])

    return max(0, min(s, 100))


def _sentiment_label(ai: dict) -> str:
    s = (ai.get("sentiment") or "neutral").lower()
    r = (ai.get("risk_level") or "medium").lower()
    if s == "bearish" or r == "extreme": return "DANGER"
    if s == "neutral" or r == "high":    return "RISK"
    return "SAFE"


def _direction(ai: dict, trend: str, tech: dict | None) -> tuple[str, str]:
    s = (ai.get("sentiment") or "neutral").lower()
    if s == "bullish":  return "BUY",  "📈"
    if s == "bearish":  return "SELL", "📉"
    if trend == "up":   return "BUY",  "📈"
    if trend == "down": return "SELL", "📉"
    if tech and tech.get("ok"):
        bull = sum([tech.get("macd_cross") == "bullish",
                    tech.get("ema_trend")  == "up",
                    tech.get("price_vs_ema20") == "above",
                    tech.get("rsi_zone")   == "oversold"])
        bear = sum([tech.get("macd_cross") == "bearish",
                    tech.get("ema_trend")  == "down",
                    tech.get("price_vs_ema20") == "below",
                    tech.get("rsi_zone")   == "overbought"])
        return ("BUY", "📈") if bull >= bear else ("SELL", "📉")
    return "SELL", "📉"


def full_analysis(price: float, prev: float | None, pair: str,
                  groq_model: str | None = None) -> dict:
    """
    Run all data fetching in parallel using threads.
    Total time = max(slowest request) instead of sum of all requests.

    ``groq_model`` — Groq chat model id. Default: ``GROQ_MODEL_SIGNALS`` (user-facing).
    Channel / admin broadcast: pass ``GROQ_MODEL_NEWS`` (higher free-tier quota).
    """
    import concurrent.futures as cf

    ref   = prev or price
    diff  = (price - ref) / ref * 100
    trend = "up" if price > ref else ("down" if price < ref else "flat")
    vol   = "normal" if abs(diff) < 0.5 else ("high" if abs(diff) < 1.0 else "chaos")

    # Run technicals, news, econ calendar in parallel (max 15s each)
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        f_tech = pool.submit(get_technicals, pair)
        f_news = pool.submit(get_news, pair)
        f_econ = pool.submit(_check_econ_calendar)

        try:
            tech = f_tech.result(timeout=15)
        except cf.TimeoutError:
            log.warning("full_analysis: technicals timeout (%s)", pair)
            tech = {"ok": False, "error": "timeout"}
        except Exception as e:
            log.warning("full_analysis: technicals error (%s): %s", pair, e)
            tech = {"ok": False, "error": str(e)[:40]}

        try:
            news = f_news.result(timeout=8)
        except Exception:
            news = ""

        try:
            econ = f_econ.result(timeout=6)
        except Exception:
            econ = {"has_danger": False, "events": []}

    use_llm = groq_model if groq_model is not None else GROQ_MODEL_SIGNALS
    # Groq call after parallel fetch (needs tech + news)
    ai    = groq_analysis(news, price, tech, trend, vol, pair, use_llm)
    score = _calc_score(tech, ai, econ, trend, vol)

    return dict(pair=pair, price=price, trend=trend, vol=vol,
                tech=tech, ai=ai, econ=econ, score=score)


# ═══════════════════════════════════════════════════════════════════
#  Formatting
# ═══════════════════════════════════════════════════════════════════

def fmt_price(price, pair: str) -> str:
    try:
        price = float(price)
    except (TypeError, ValueError):
        return str(price)
    if pair in ("BTCUSD", "BNBUSD"):
        return f"{price:,.0f}"
    elif pair in ("ETHUSD", "SOLUSD", "XAGUSD"):
        return f"{price:,.2f}"
    elif pair in ("XRPUSD", "ADAUSD"):
        return f"{price:,.4f}"
    else:   # XAUUSD
        return f"{price:,.2f}"


def score_bar(s: int) -> str:
    filled = round(s / 10)
    return "█" * filled + "░" * (10 - filled)


def post_type_for_hour(h: int) -> str:
    return {6: "morning", 12: "midday", 18: "evening"}.get(h, "scheduled")


def build_analysis_text(a: dict) -> str:
    pair  = a["pair"];  cfg = PAIRS[pair]
    ai    = a["ai"];    tech = a["tech"]
    econ  = a["econ"];  score = a["score"];  price = a["price"]
    lbl   = _sentiment_label(ai)
    si    = {"SAFE": "🟢", "RISK": "🟡", "DANGER": "🔴"}.get(lbl, "⚪")
    dr, de = _direction(ai, a["trend"], tech)
    verdict = (
        "✅ *GOOD setup*"  if score >= 75 else
        "⚠️ *NEUTRAL*"     if score >= 50 else
        "🟠 *WEAK setup*"  if score >= 35 else
        "🔴 *AVOID*"
    )
    lines = [
        f"{cfg['emoji']} *PRE-TRADE {cfg['name']}*",
        "─" * 28,
        f"💰 Price: *{fmt_price(price, pair)}*",
        f"{'🟢' if dr == 'BUY' else '🔴'} Direction: *{dr}* {de}",
        f"📈 Trend: `{a['trend'].upper()}`  |  Vol: `{a['vol'].upper()}`",
        f"{si} AI: `{(ai.get('sentiment') or '?').upper()}`  "
        f"Confidence: *{ai.get('confidence', '?')}%*",
        f"⚖️ Risk: `{(ai.get('risk_level') or 'MEDIUM').upper()}`",
        "",
        f"🎯 Entry: *{ai.get('optimal_entry', price)}*",
        f"🛑 SL: *{ai.get('stop_loss', 'N/A')}*   TP: *{ai.get('take_profit', 'N/A')}*",
        f"📐 R/R: *{ai.get('risk_reward', 'N/A')}*",
    ]
    if tech.get("ok"):
        lines += [
            "",
            f"📊 RSI: *{tech['rsi']}* ({tech['rsi_zone']})  MACD: *{tech['macd_cross']}*",
            f"EMA20: *{tech['ema20']}*  EMA50: *{tech['ema50']}*  |  "
            f"Price {tech['price_vs_ema20']} EMA",
        ]
    if econ.get("has_danger"):
        lines += [
            "",
            "⚠️ *High-impact USD news today!*",
            "\n".join(f"• {e}" for e in econ.get("events", [])),
        ]
    lines += ["", "─" * 28,
              f"📊 Score: `{score_bar(score)}`  *{score}/100*", "", verdict]
    return "\n".join(lines)


def groq_channel_post(a: dict, post_type: str) -> str:
    pair  = a["pair"];  cfg = PAIRS[pair]
    ai    = a["ai"];    score = a["score"];  price = a["price"]
    lbl   = _sentiment_label(ai)
    si    = {"SAFE": "🟢", "RISK": "🟡", "DANGER": "🔴"}.get(lbl, "⚪")
    dr, de = _direction(ai, a["trend"], a.get("tech"))

    header = {
        "morning":   "☀️ *Morning Overview*",
        "midday":    "🌤 *Midday Signal*",
        "evening":   "🌙 *Evening Summary*",
        "manual":    "📡 *Signal*",
        "scheduled": "📊 *Analysis*",
    }.get(post_type, "📊 *Analysis*")

    dir_emoji = "🟢" if dr == "BUY" else "🔴"

    # TP1 / TP2 / TP3
    entry  = float(ai.get("optimal_entry") or price)
    sl     = float(ai.get("stop_loss")     or _make_sl_tp(price, dr, cfg["sl_pct"], cfg["tp_pct"])[0])
    tp_pct = cfg["tp_pct"]
    sl_pct = cfg["sl_pct"]
    if dr == "BUY":
        tp1 = round(entry * (1 + tp_pct * 0.5 / 100), 5)
        tp2 = round(entry * (1 + tp_pct / 100), 5)
        tp3 = round(entry * (1 + tp_pct * 1.8 / 100), 5)
    else:
        tp1 = round(entry * (1 - tp_pct * 0.5 / 100), 5)
        tp2 = round(entry * (1 - tp_pct / 100), 5)
        tp3 = round(entry * (1 - tp_pct * 1.8 / 100), 5)

    verdict = (
        "✅ *Good entry opportunity*" if score >= 75 else
        "⚠️ *Wait for better conditions*" if score >= 50 else
        "🔴 *Stay on the sidelines*"
    )

    div = "─" * 28
    lines = [
        f"{header} | {cfg['emoji']} {cfg['name']}",
        div,
        f"💰 Price:  *{fmt_price(price, pair)}*",
        f"{dir_emoji} Signal: *{dr}* {de}",
        f"{si} Sentiment: *{(ai.get('sentiment') or '?').upper()}*  |  "
        f"Confidence: *{ai.get('confidence', '?')}%*",
        "",
        f"📐 *Trade Setup:*",
        f"   Entry:  *{fmt_price(entry, pair)}*",
        f"   SL:     *{fmt_price(sl, pair)}*",
        f"   TP1:    *{fmt_price(tp1, pair)}*",
        f"   TP2:    *{fmt_price(tp2, pair)}*",
        f"   TP3:    *{fmt_price(tp3, pair)}*",
        "",
        f"📊 Score: `{score_bar(score)}`  *{score}/100*",
        "",
        verdict,
        div,
        f"🤖 {BOT_USERNAME}",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  News RSS for articles
# ═══════════════════════════════════════════════════════════════════

_RSS_FEEDS = [
    ("Reuters Markets",   "https://feeds.reuters.com/reuters/businessNews"),
    ("MarketWatch",       "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("CoinDesk",          "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CryptoNews",        "https://cryptonews.com/news/feed/"),
    ("Investing.com",     "https://www.investing.com/rss/news_25.rss"),
    ("FXStreet",          "https://www.fxstreet.com/rss/news"),
    ("ForexLive",         "https://www.forexlive.com/feed/news"),
    ("Kitco Gold",        "https://www.kitco.com/rss/news/kitconews.rss"),
]
_NEWS_KW = ["gold", "xau", "bitcoin", "btc", "crypto", "ethereum",
            "eth", "fed", "inflation", "market", "trading", "usd"]


def get_news_rss() -> list[dict]:
    cutoff  = datetime.now(UTC) - timedelta(hours=48)
    results = []
    for source, url in _RSS_FEEDS:
        try:
            r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:10]:
                title   = (item.findtext("title") or "").strip()
                summary = (item.findtext("description") or "").strip()[:200]
                pub_str = item.findtext("pubDate") or ""
                try:
                    pub_dt = parsedate_to_datetime(pub_str)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=UTC)
                    else:
                        pub_dt = pub_dt.astimezone(UTC)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass
                if any(k in (title + " " + summary).lower() for k in _NEWS_KW):
                    results.append({"title": title, "summary": summary, "source": source})
        except Exception as e:
            log.debug("RSS (%s): %s", source, e)
    return results[:8]


def get_news_for_article(query: str) -> str:
    items = get_news_rss()
    if items:
        return "\n".join(f"[{i['source']}] {i['title']}. {i['summary']}" for i in items[:5])
    try:
        from_dt = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
        r = requests.get(
            f"https://newsapi.org/v2/everything?q={query.replace(' ', '%20')}"
            f"&from={from_dt}&sortBy=publishedAt&pageSize=5&language=en&apiKey={NEWS_API}",
            timeout=6,
        )
        r.raise_for_status()
        arts = r.json().get("articles", [])[:5]
        if arts:
            return "\n".join(
                f"[{a.get('source', {}).get('name', '?')}] "
                f"{a.get('title', '')}. {(a.get('description') or '')[:150]}"
                for a in arts
            )
    except Exception as e:
        log.warning("NewsAPI article: %s", e)
    return ""


# ═══════════════════════════════════════════════════════════════════
#  Article generation  (single Groq call, no internal retries)
# ═══════════════════════════════════════════════════════════════════

EDU_TOPICS = [
    "What is RSI and how to use it in trading",
    "What is MACD and how to read its signals",
    "Stop Loss and Take Profit — why they are mandatory",
    "Support and resistance — how to find key levels",
    "How to read candlestick charts — basics for beginners",
    "What is a trend and how to identify it",
    "Trading psychology — why emotions destroy profits",
    "What is leverage and how to avoid overusing it",
    "Gold as a safe-haven asset — why traders choose XAU",
    "What is volatility and how it affects trading",
    "How to calculate position size correctly",
    "Technical vs fundamental analysis — the difference",
    "What is Parabolic SAR and how to use it",
    "Bollinger Bands explained for traders",
    "How to trade breakouts correctly",
    "Risk management — the 1% rule explained",
    "What drives silver prices — XAG fundamentals",
    "How to use moving averages in trading",
]

NEWS_TOPICS = [
    "gold XAU price Fed inflation safe haven",
    "bitcoin BTC price ETF crypto market",
    "ethereum ETH DeFi crypto news",
    "silver XAG price industrial demand",
    "XRP Ripple SEC crypto regulation",
    "Solana SOL crypto ecosystem news",
    "BNB Binance crypto exchange news",
    "Cardano ADA blockchain crypto",
    "central bank interest rates dollar DXY",
    "crypto market sentiment bitcoin altcoins",
    "gold inflation hedge geopolitical risk",
    "Federal Reserve policy interest rates markets",
]


def groq_article(topic_type: str, topic: str) -> str:
    """Generate an article post. For news — uses real RSS headline. Raises on error."""
    if topic_type == "news":
        # Pick the freshest relevant RSS article as the base
        items = get_news_rss()
        if items:
            # Try to find one matching the topic keywords
            kws = topic.lower().split()
            scored = []
            for it in items:
                text = (it["title"] + " " + it["summary"]).lower()
                scored.append((sum(k in text for k in kws), it))
            scored.sort(key=lambda x: x[0], reverse=True)
            best = scored[0][1]
            news_block = (
                f"SOURCE: {best['source']}\n"
                f"HEADLINE: {best['title']}\n"
                f"SUMMARY: {best['summary']}\n"
            )
        else:
            # Fallback to NewsAPI
            news_raw = get_news_for_article(topic)
            news_block = f"Recent news:\n{news_raw[:600]}\n" if news_raw else ""

        prompt = (
            f"You are a financial news editor for a Telegram trading channel.\n"
            f"Based on this real news item, write a post:\n\n"
            f"{news_block}\n"
            "Requirements:\n"
            "- Headline: one bold sentence summarising the news\n"
            "- What happened: 2 sentences (facts, numbers)\n"
            "- Why it matters for traders: 2 sentences (price impact, which assets)\n"
            "- Market reaction: what to watch (1-2 sentences)\n"
            "- 📌 Trader tip: one concrete actionable takeaway\n\n"
            "Length: 100-150 words. Use *bold* ONLY for the headline. No hashtags."
        )
    else:
        prompt = (
            f"You are an experienced trader writing for a Telegram trading channel.\n"
            f"Topic: {topic}\n\n"
            "Write an educational post:\n"
            "- *Bold headline* (1 sentence)\n"
            "- What it is: 1-2 sentences\n"
            "- How it works: concrete example with numbers (2-3 sentences)\n"
            "- Why traders need it: 1-2 sentences\n"
            "- ⚠️ Common mistake: 1 sentence\n"
            "- 📌 Practical tip: 1 sentence\n\n"
            "Length: 120-160 words. Use *bold* ONLY for the headline. No hashtags."
        )
    try:
        result = _groq_client().chat.completions.create(
            model=GROQ_MODEL_NEWS, timeout=GROQ_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=500,
        ).choices[0].message.content.strip()
    except Exception as groq_err:
        if "429" in str(groq_err) or "rate_limit" in str(groq_err).lower():
            log.info("Groq rate limit — trying OpenRouter then Gemini for article")
            result = _groq_fallback_article_text(prompt, max_tokens=500)
        else:
            raise
    return result


def format_article_post(topic_type: str, body: str) -> str:
    div = "─" * 30
    if topic_type == "edu":
        header = f"📚 *Educational Post*\n{div}"
        footer = f"\n{div}\n💡 _Learn more → {BOT_USERNAME}_"
    else:
        header = f"📰 *Market News*\n{div}"
        footer = f"\n{div}\n📊 _Signals & analysis → {BOT_USERNAME}_"
    return f"{header}\n\n{body}\n{footer}"


# ═══════════════════════════════════════════════════════════════════
#  Image generation (matplotlib)
# ═══════════════════════════════════════════════════════════════════

# Map edu topics to infographic type
_EDU_CHART_MAP = {
    "rsi":          "rsi",
    "macd":         "macd",
    "stop loss":    "sl_tp",
    "take profit":  "sl_tp",
    "support":      "support_resistance",
    "resistance":   "support_resistance",
    "candlestick":  "candlestick",
    "trend":        "trend",
    "position size":"position_size",
    "risk":         "risk_reward",
    "leverage":     "leverage",
    "psychology":   "psychology",
    "volatility":   "volatility",
}


def _edu_chart_type(topic: str) -> str:
    t = topic.lower()
    for kw, chart in _EDU_CHART_MAP.items():
        if kw in t:
            return chart
    return "generic"


def _gen_price_chart(pair: str) -> io.BytesIO | None:
    """Generate a candlestick chart with volume for the given pair."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import yfinance as yf
        import numpy as np

        ticker = PAIRS[pair]["yahoo"]
        df = yf.download(ticker, period="5d", interval="15m",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 10:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = df.tail(48)  # last ~12 hours of 15m candles

        opens  = df["Open"].values.flatten()
        highs  = df["High"].values.flatten()
        lows   = df["Low"].values.flatten()
        closes = df["Close"].values.flatten()
        vols   = df["Volume"].values.flatten() if "Volume" in df.columns else None
        xs     = range(len(df))

        # ── Style ──────────────────────────────────────────────
        bg      = "#0d1117"
        up_col  = "#26a69a"
        dn_col  = "#ef5350"
        txt_col = "#c9d1d9"
        grid_col= "#21262d"

        fig_h = 5.5 if vols is not None else 4.5
        fig, axes = plt.subplots(
            2 if vols is not None else 1, 1,
            figsize=(10, fig_h),
            gridspec_kw={"height_ratios": [3, 1]} if vols is not None else {},
            facecolor=bg,
        )
        ax = axes[0] if vols is not None else axes
        ax.set_facecolor(bg)

        # Candles
        width_body = 0.6
        for i in range(len(df)):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            col = up_col if c >= o else dn_col
            ax.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=2)
            rect = mpatches.FancyBboxPatch(
                (i - width_body / 2, min(o, c)),
                width_body, abs(c - o) or (h - l) * 0.01,
                boxstyle="square,pad=0",
                linewidth=0, facecolor=col, zorder=3,
            )
            ax.add_patch(rect)

        # EMA 20
        try:
            import pandas as pd
            ema20 = pd.Series(closes).ewm(span=20).mean()
            ax.plot(xs, ema20, color="#f0a500", linewidth=1.2,
                    linestyle="--", alpha=0.8, label="EMA 20", zorder=4)
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8, loc="upper left")
        except Exception:
            pass

        # Grid & labels
        ax.set_xlim(-1, len(df))
        ax.set_facecolor(bg)
        ax.tick_params(colors=txt_col, labelsize=7)
        ax.grid(True, color=grid_col, linewidth=0.5, zorder=1)
        for spine in ax.spines.values():
            spine.set_edgecolor(grid_col)

        cfg = PAIRS[pair]
        ax.set_title(
            f"{cfg['emoji']} {cfg['name']}  |  15m chart",
            color=txt_col, fontsize=11, fontweight="bold", pad=8,
        )

        # Last price label
        last_price = closes[-1]
        ax.axhline(last_price, color="#ffffff", linewidth=0.6, linestyle=":", alpha=0.5)
        ax.annotate(
            f"  {fmt_price(last_price, pair)}",
            xy=(len(df) - 1, last_price),
            color="#ffffff", fontsize=8, va="center",
        )

        # Volume subplot
        if vols is not None:
            ax_v = axes[1]
            ax_v.set_facecolor(bg)
            colors_v = [up_col if closes[i] >= opens[i] else dn_col for i in xs]
            ax_v.bar(xs, vols, color=colors_v, alpha=0.6, width=0.8)
            ax_v.set_xlim(-1, len(df))
            ax_v.tick_params(colors=txt_col, labelsize=6)
            ax_v.set_ylabel("Vol", color=txt_col, fontsize=7)
            ax_v.grid(True, color=grid_col, linewidth=0.4)
            for spine in ax_v.spines.values():
                spine.set_edgecolor(grid_col)

        plt.tight_layout(pad=0.8)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, facecolor=bg,
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf
    except ImportError:
        log.warning("matplotlib/yfinance not installed — skipping chart")
        return None
    except Exception as e:
        log.warning("Price chart error (%s): %s", pair, e)
        return None


def _gen_edu_infographic(topic: str) -> io.BytesIO | None:
    """Generate a simple educational infographic based on topic."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        chart_type = _edu_chart_type(topic)
        bg      = "#0d1117"
        txt_col = "#c9d1d9"
        up_col  = "#26a69a"
        dn_col  = "#ef5350"
        acc_col = "#f0a500"
        grid_col= "#21262d"

        fig, ax = plt.subplots(figsize=(10, 5), facecolor=bg)
        ax.set_facecolor(bg)
        for spine in ax.spines.values():
            spine.set_edgecolor(grid_col)
        ax.tick_params(colors=txt_col, labelsize=8)
        ax.grid(True, color=grid_col, linewidth=0.5)

        x = np.linspace(0, 4 * np.pi, 200)

        if chart_type == "rsi":
            # Simulated price + RSI
            price = 100 + 15 * np.sin(x) + np.cumsum(np.random.randn(200) * 0.3)
            # Simple RSI approximation
            delta = np.diff(price, prepend=price[0])
            gain  = np.where(delta > 0, delta, 0)
            loss  = np.where(delta < 0, -delta, 0)
            avg_g = np.convolve(gain, np.ones(14)/14, mode="same")
            avg_l = np.convolve(loss, np.ones(14)/14, mode="same") + 1e-9
            rsi   = 100 - 100 / (1 + avg_g / avg_l)

            ax2 = ax.twinx()
            ax.plot(x, price, color=acc_col, linewidth=1.5, label="Price")
            ax2.plot(x, rsi, color="#7b68ee", linewidth=1.2, label="RSI(14)")
            ax2.axhline(70, color=dn_col, linewidth=0.8, linestyle="--", alpha=0.7)
            ax2.axhline(30, color=up_col, linewidth=0.8, linestyle="--", alpha=0.7)
            ax2.fill_between(x, 70, rsi, where=(rsi > 70),
                             color=dn_col, alpha=0.15, label="Overbought")
            ax2.fill_between(x, 30, rsi, where=(rsi < 30),
                             color=up_col, alpha=0.15, label="Oversold")
            ax2.set_ylim(0, 100)
            ax2.tick_params(colors=txt_col, labelsize=8)
            ax2.set_ylabel("RSI", color=txt_col, fontsize=9)
            ax2.yaxis.label.set_color(txt_col)
            ax.set_title("RSI Indicator — Overbought & Oversold Zones",
                         color=txt_col, fontsize=12, fontweight="bold")
            lines1, labels1 = ax.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax.legend(lines1 + lines2, labels1 + labels2,
                      facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8, loc="upper left")

        elif chart_type == "macd":
            price  = 100 + np.cumsum(np.random.randn(200) * 0.5)
            ema12  = np.convolve(price, np.ones(12)/12, mode="same")
            ema26  = np.convolve(price, np.ones(26)/26, mode="same")
            macd   = ema12 - ema26
            signal = np.convolve(macd, np.ones(9)/9, mode="same")
            hist   = macd - signal
            ax.bar(x, hist, color=np.where(hist >= 0, up_col, dn_col),
                   alpha=0.6, width=x[1]-x[0], label="Histogram")
            ax.plot(x, macd,   color=acc_col,  linewidth=1.5, label="MACD")
            ax.plot(x, signal, color="#7b68ee", linewidth=1.2, label="Signal")
            ax.axhline(0, color=txt_col, linewidth=0.5, linestyle="--")
            ax.set_title("MACD — Crossover Signals",
                         color=txt_col, fontsize=12, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8)

        elif chart_type == "sl_tp":
            entry  = 100.0
            sl_buy = 97.0;  tp_buy = 106.0
            sl_sel = 103.0; tp_sel = 94.0
            xs_b   = [0, 1]; xs_s = [2, 3]
            # BUY trade
            ax.barh(0, tp_buy - entry, left=entry, color=up_col,
                    alpha=0.4, height=0.4, label=f"TP Buy ({tp_buy})")
            ax.barh(0, sl_buy - entry, left=entry, color=dn_col,
                    alpha=0.4, height=0.4, label=f"SL Buy ({sl_buy})")
            # SELL trade
            ax.barh(-1, entry - tp_sel, left=tp_sel, color=up_col,
                    alpha=0.4, height=0.4, label=f"TP Sell ({tp_sel})")
            ax.barh(-1, entry - sl_sel, left=sl_sel, color=dn_col,
                    alpha=0.4, height=0.4, label=f"SL Sell ({sl_sel})")
            ax.axvline(entry, color=acc_col, linewidth=1.5,
                       linestyle="--", label=f"Entry ({entry})")
            ax.set_yticks([0, -1])
            ax.set_yticklabels(["BUY trade", "SELL trade"],
                               color=txt_col, fontsize=10)
            ax.set_title("Stop Loss & Take Profit — BUY vs SELL",
                         color=txt_col, fontsize=12, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8, loc="upper right")

        elif chart_type == "support_resistance":
            price  = 100 + 8 * np.sin(x) + np.cumsum(np.random.randn(200) * 0.2)
            support  = np.percentile(price, 20)
            resistance = np.percentile(price, 80)
            ax.plot(x, price, color=acc_col, linewidth=1.4, label="Price")
            ax.axhline(support,    color=up_col, linewidth=1.2,
                       linestyle="--", label=f"Support ≈ {support:.1f}")
            ax.axhline(resistance, color=dn_col, linewidth=1.2,
                       linestyle="--", label=f"Resistance ≈ {resistance:.1f}")
            ax.fill_between(x, support - 1, support + 1,
                            color=up_col, alpha=0.15)
            ax.fill_between(x, resistance - 1, resistance + 1,
                            color=dn_col, alpha=0.15)
            ax.set_title("Support & Resistance Levels",
                         color=txt_col, fontsize=12, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8)

        elif chart_type == "risk_reward":
            ratios = [1, 1.5, 2, 2.5, 3]
            wins   = [50, 50, 50, 50, 50]
            needed = [r / (1 + r) * 100 for r in ratios]
            bars = ax.bar([f"1:{r}" for r in ratios], needed,
                          color=[up_col if n < 50 else dn_col for n in needed],
                          alpha=0.8, edgecolor=grid_col)
            ax.axhline(50, color=acc_col, linewidth=1,
                       linestyle="--", label="50% win rate")
            for bar, val in zip(bars, needed):
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + 1,
                        f"{val:.0f}% needed",
                        ha="center", color=txt_col, fontsize=8)
            ax.set_xlabel("Risk:Reward ratio", color=txt_col)
            ax.set_ylabel("Min win rate needed (%)", color=txt_col)
            ax.set_title("Risk/Reward — Minimum Win Rate Required",
                         color=txt_col, fontsize=12, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8)

        elif chart_type == "trend":
            price = 100 + np.cumsum(np.random.randn(200) * 0.4) + np.linspace(0, 15, 200)
            ema20 = np.convolve(price, np.ones(20)/20, mode="same")
            ema50 = np.convolve(price, np.ones(50)/50, mode="same")
            ax.plot(x, price, color=txt_col,  linewidth=1,   alpha=0.6, label="Price")
            ax.plot(x, ema20, color=acc_col,  linewidth=1.4, label="EMA 20")
            ax.plot(x, ema50, color="#7b68ee", linewidth=1.4, label="EMA 50")
            ax.fill_between(x, ema20, ema50,
                            where=(ema20 > ema50), color=up_col, alpha=0.1)
            ax.fill_between(x, ema20, ema50,
                            where=(ema20 < ema50), color=dn_col, alpha=0.1)
            ax.set_title("Trend Identification with EMA 20 & EMA 50",
                         color=txt_col, fontsize=12, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8)

        else:
            # Generic: price with EMA
            price = 100 + 10 * np.sin(x) + np.cumsum(np.random.randn(200) * 0.3)
            ema   = np.convolve(price, np.ones(20)/20, mode="same")
            ax.plot(x, price, color=acc_col,  linewidth=1.4, label="Price", alpha=0.8)
            ax.plot(x, ema,   color="#7b68ee", linewidth=1.2, label="EMA 20")
            ax.set_title(f"Market Chart — {topic[:50]}",
                         color=txt_col, fontsize=11, fontweight="bold")
            ax.legend(facecolor=bg, edgecolor=grid_col,
                      labelcolor=txt_col, fontsize=8)

        ax.set_xlabel("Time", color=txt_col, fontsize=9)
        ax.xaxis.label.set_color(txt_col)
        ax.yaxis.label.set_color(txt_col)

        plt.tight_layout(pad=0.8)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, facecolor=bg,
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf
    except ImportError:
        log.warning("matplotlib not installed — pip install matplotlib")
        return None
    except Exception as e:
        log.warning("Edu infographic error: %s", e)
        return None


def _gen_news_chart(topic: str) -> io.BytesIO | None:
    """
    For news articles: generate a price chart for the most relevant pair.
    Falls back to XAUUSD if no match.
    """
    t = topic.lower()
    if "bitcoin" in t or "btc" in t:
        pair = "BTCUSD"
    elif "ethereum" in t or "eth" in t:
        pair = "ETHUSD"
    else:
        pair = "XAUUSD"
    return _gen_price_chart(pair)


async def send_article_with_image(
    bot, chat_id: int, topic_type: str, topic: str, caption: str
) -> None:
    """
    Send article with the best available image:
    1. Generated chart/infographic (matplotlib)
    2. Unsplash fallback photo
    3. Text only
    """
    loop = asyncio.get_event_loop()

    # 1. Try generating chart in thread executor
    try:
        if topic_type == "news":
            buf = await loop.run_in_executor(None, _gen_news_chart, topic)
        else:
            buf = await loop.run_in_executor(None, _gen_edu_infographic, topic)
    except Exception as e:
        log.warning("Chart generation error: %s", e)
        buf = None

    if buf is not None:
        try:
            from telegram import InputFile
            await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(buf, filename="chart.png"),
                caption=fix_markdown(caption),
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            log.warning("Generated chart send failed: %s — trying Unsplash", e)

    # 2. Unsplash fallback
    unsplash_url = _pick_unsplash(topic_type, topic)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=unsplash_url,
            caption=fix_markdown(caption),
            parse_mode="Markdown",
        )
        return
    except Exception as e:
        log.warning("Unsplash fallback failed: %s — sending text only", e)

    # 3. Text only
    await safe_send(bot, chat_id, caption)
    for ch in ("*", "_", "`"):
        if text.count(ch) % 2 != 0:
            text += ch
    opens  = [m.start() for m in re.finditer(r"\[", text)]
    closes = [m.start() for m in re.finditer(r"\]", text)]
    if len(opens) > len(closes):
        for idx in reversed(opens[len(closes):]):
            text = text[:idx] + text[idx + 1:]
    return text


async def safe_send(bot, chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(
            chat_id, fix_markdown(text), parse_mode="Markdown", **kwargs
        )
    except BadRequest as e:
        if "parse" in str(e).lower() or "entity" in str(e).lower():
            plain = re.sub(r"[*_`]", "", text)
            return await bot.send_message(chat_id, plain, **kwargs)
        raise


async def safe_send_photo(bot, chat_id: int, photo_url: str, caption: str) -> bool:
    """Send photo + caption. Returns True on success."""
    try:
        await bot.send_photo(
            chat_id=chat_id, photo=photo_url,
            caption=fix_markdown(caption), parse_mode="Markdown",
        )
        return True
    except Exception as e:
        log.warning("Photo send failed (%s): %s", chat_id, e)
        return False


async def safe_edit(q, text: str, markup=None, **kwargs):
    try:
        await q.edit_message_text(
            fix_markdown(text), parse_mode="Markdown", reply_markup=markup, **kwargs,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("safe_edit: %s", e)


async def safe_callback_answer(q, **kwargs) -> None:
    """Acknowledge a callback query; ignore expired IDs after long blocking work."""
    try:
        await q.answer(**kwargs)
    except BadRequest as e:
        low = str(e).lower()
        if "too old" in low or "invalid" in low or "query id" in low:
            log.debug("Callback query expired, skipping answer: %s", e)
        else:
            raise


# ═══════════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════════

PLAN_EMOJI = {"trial": "🔬", "basic": "⭐", "pro": "💎", "diamond": "💠", "admin": "👑", "expired": "❌"}


def plan_label(p: str) -> str:
    return {"trial": "Trial", "basic": "Basic", "pro": "Pro", "diamond": "Diamond",
            "admin": "Admin", "expired": "Expired"}.get(p, p)


def kb_main(plan: str = "trial", pair: str = DEFAULT_PAIR, deep_left: int | None = None) -> InlineKeyboardMarkup:
    cfg = PAIRS[pair]
    rows = [
        [InlineKeyboardButton(f"🔀 Pair: {cfg['emoji']} {cfg['name']}", callback_data="choose_pair")],
        [InlineKeyboardButton("▶️ Analyse & Enter", callback_data="start")],
    ]
    if plan in ("basic", "pro", "diamond", "admin", "trial"):
        rows.append([
            InlineKeyboardButton("⏹ Stop",  callback_data="stop"),
            InlineKeyboardButton("🔄 Reset", callback_data="reset"),
        ])
        rows.append([InlineKeyboardButton("📊 Trade Status", callback_data="status")])
    if plan in ("trial", "diamond", "admin"):
        if plan == "admin":
            deep_label = "🧠 Deep Analysis"
        elif plan == "trial":
            left = deep_left if deep_left is not None else 1
            deep_label = f"🧠 Deep Analysis ({left}/1)"
        else:
            left = deep_left if deep_left is not None else DEEP_ANALYSIS_DAILY_LIMIT
            deep_label = f"🧠 Deep Analysis ({left}/{DEEP_ANALYSIS_DAILY_LIMIT})"
        if plan in ("diamond", "admin"):
            rows.append([
                InlineKeyboardButton(deep_label, callback_data="deepanalysis_menu"),
                InlineKeyboardButton("📸 Chart AI", callback_data="chart_ai"),
            ])
        else:
            rows.append([InlineKeyboardButton(deep_label, callback_data="deepanalysis_menu")])
    rows.append([
        InlineKeyboardButton("💳 Subscription", callback_data="sub_menu"),
        InlineKeyboardButton("🤝 Refer & Earn",  callback_data="refer"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_main_for(cid: int, plan: str, pair: str = DEFAULT_PAIR) -> InlineKeyboardMarkup:
    """Build kb_main with correct deep_left counter for the given user."""
    if plan in ("trial", "diamond"):
        used = db_deepanalysis_count_today(cid)
        limit = 1 if plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
        deep_left = max(0, limit - used)
    else:
        deep_left = None
    return kb_main(plan, pair, deep_left=deep_left)


def kb_pairs(current_pair: str, plan: str) -> InlineKeyboardMarkup:
    rows = []
    for pid, cfg in PAIRS.items():
        accessible = plan in cfg["plans"]
        mark  = "✅" if pid == current_pair else ("🔒" if not accessible else "")
        label = f"{mark} {cfg['emoji']} {cfg['name']}" + (" (Pro)" if not accessible else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"pair_{pid}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_sub() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"⭐ Basic — {PRICE_BASIC}⭐/mo (~$5)",            callback_data="buy_basic_1")],
        [InlineKeyboardButton(f"⭐ Basic — {PRICE_BASIC_3}⭐/3mo (~$12.5) 🔥",   callback_data="buy_basic_3")],
        [InlineKeyboardButton(f"💎 Pro   — {PRICE_PRO}⭐/mo (~$9.99)",          callback_data="buy_pro_1")],
        [InlineKeyboardButton(f"💎 Pro   — {PRICE_PRO_3}⭐/3mo (~$25) 🔥",      callback_data="buy_pro_3")],
        [InlineKeyboardButton(f"💠 Diamond — {PRICE_DIAMOND}⭐/mo (~$19.99)",   callback_data="buy_diamond_1")],
        [InlineKeyboardButton(f"💠 Diamond — {PRICE_DIAMOND_3}⭐/3mo (~$49.99) 🔥", callback_data="buy_diamond_3")],
    ]
    # Optional crypto payments (NOWPayments)
    if NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL and NOWPAYMENTS_IPN_SECRET:
        rows += [
            [InlineKeyboardButton("₮ Pay with Crypto (USDT TRC20)", callback_data="crypto_menu")],
        ]
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_confirm(opt: float, pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Enter now",                        callback_data="confirm_now")],
        [InlineKeyboardButton(f"⏳ Wait for {fmt_price(opt, pair)}", callback_data=f"wait_{opt}")],
        [InlineKeyboardButton("❌ Cancel",                           callback_data="cancel")],
        [InlineKeyboardButton("🔄 Refresh analysis",                 callback_data="refresh_analysis")],
    ])


def sub_info_text(acc: dict) -> str:
    plan = acc["plan"];  dl = acc["days_left"]
    lines = [f"💳 *Plan: {PLAN_EMOJI.get(plan, '?')} {plan_label(plan)}*", ""]
    if plan == "trial":
        lines += [f"🔬 Trial: *{dl} days* left", "",
                  "🥇 XAU/USD — ✅", "₿ BTC — 🔒", "Ξ ETH — 🔒", "",
                  "🧠 Deep Analysis — ✅ _(1/day)_", ""]
    elif plan == "basic":
        lines += [f"⭐ Basic: *{dl} days* left", "",
                  "🥇 XAU/USD — ✅", "🥈 XAG/USD — ✅",
                  "₿ BTC — 🔒", "Ξ ETH — 🔒", "◎ SOL — 🔒",
                  "✕ XRP — 🔒", "🔶 BNB — 🔒", "🔵 ADA — 🔒", ""]
    elif plan == "pro":
        lines += [f"💎 Pro: *{dl} days* left", "",
                  "🥇 XAU — ✅", "🥈 XAG — ✅",
                  "₿ BTC — ✅", "Ξ ETH — ✅", "◎ SOL — ✅",
                  "✕ XRP — ✅", "🔶 BNB — ✅", "🔵 ADA — ✅",
                  "✅ Auto-signals", ""]
    elif plan == "diamond":
        lines += [f"💠 Diamond: *{dl} days* left", "",
                  "🥇 XAU — ✅", "🥈 XAG — ✅",
                  "₿ BTC — ✅", "Ξ ETH — ✅", "◎ SOL — ✅",
                  "✕ XRP — ✅", "🔶 BNB — ✅", "🔵 ADA — ✅",
                  "✅ Auto-signals (priority)", "✅ Chart AI screenshot analysis",
                  "✅ Priority alerts (lower threshold)", ""]
    elif plan in ("expired", "none"):
        lines += ["❌ *Subscription expired*", ""]
    lines += [
        "─" * 30,
        "*⭐ Basic — $5/mo*",
        "  • XAU/USD signals",
        "  • AI pre-trade analysis",
        "  • SL / TP monitoring",
        f"  1 mo — *{PRICE_BASIC}⭐* (~$5)",
        f"  3 mo — *{PRICE_BASIC_3}⭐* (~$12.5) 🔥 _save ~17%_",
        "",
        "*💎 Pro — $9.99/mo*",
        "  • Everything in Basic +",
        "  • BTC/USD and ETH/USD",
        "  • 24/7 auto-signals",
        "  • Priority alerts",
        f"  1 mo — *{PRICE_PRO}⭐* (~$9.99)",
        f"  3 mo — *{PRICE_PRO_3}⭐* (~$25) 🔥 _save ~17%_",
        "",
        "*💠 Diamond — $19.99/mo*",
        "  • Everything in Pro +",
        "  • 📸 Chart AI screenshot analysis",
        "  • Priority signals (lower threshold)",
        "  • Faster auto-signal cooldown",
        f"  1 mo — *{PRICE_DIAMOND}⭐* (~$19.99)",
        f"  3 mo — *{PRICE_DIAMOND_3}⭐* (~$49.99) 🔥 _save ~17%_",
        "",
        "💡 _First week free for new users_",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  Command handlers
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid    = update.effective_chat.id
    u      = update.effective_user
    args   = context.args  # e.g. ["ref_123456"] or ["youtube"] or []
    is_new = False

    with db_connect() as c:
        exists = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (cid,)).fetchone()
        is_new = exists is None

    db_upsert_user(cid, u.username or "", u.first_name or "")

    # ── Parse start parameter ────────────────────────────────
    source     = None
    referrer_id = None

    if args:
        param = args[0]
        if param.startswith("ref_"):
            # Referral link from another user
            try:
                referrer_id = int(param[4:])
                source = "referral"
            except ValueError:
                pass
        else:
            # UTM source (youtube, instagram, etc.)
            source = param[:32]

    # ── Save UTM source (only for new users) ────────────────
    if is_new and source:
        db_save_utm(cid, source)

    # ── Register referral and give bonus ────────────────────
    if is_new and referrer_id and referrer_id != cid:
        registered = db_register_referral(referrer_id, cid, source or "ref")
        if registered:
            # Give bonus days to referrer immediately on activation
            bonus = db_give_referral_bonus(referrer_id, cid)
            if bonus > 0:
                try:
                    ref_u = await context.bot.get_chat(referrer_id)
                    await context.bot.send_message(
                        referrer_id,
                        f"🎁 *+{bonus} days added to your plan!*\n\n"
                        f"Someone joined using your referral link.\n"
                        f"Keep sharing to earn more free days! 🚀\n\n"
                        f"/refer — see your referral stats",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            log.info("Referral: %s → %s (source=%s)", referrer_id, cid, source)

    acc   = db_access(cid)
    plan  = acc["plan"]

    # Show prices for pairs accessible on this plan
    price_lines = []
    for pid, cfg in PAIRS.items():
        if plan in cfg["plans"]:
            p = get_price(pid)
            if p:
                price_lines.append(f"{cfg['emoji']} {cfg['name']}: *{fmt_price(p, pid)}*")
    prices_text = "\n".join(price_lines) if price_lines else ""

    # Welcome message differs for referred users
    if is_new and referrer_id:
        welcome = (
            f"👋 *Welcome! You were invited by a friend.*\n\n"
            f"{prices_text}\n\n"
            f"Plan: {PLAN_EMOJI.get(plan, '?')} *{plan_label(plan)}*  ({acc['days_left']} days)\n\n"
            f"Choose a pair and tap ▶️ Analyse & Enter"
        )
    else:
        welcome = (
            f"🤖 *Gold & Crypto AI Signals*\n\n"
            f"{prices_text}\n\n"
            f"Plan: {PLAN_EMOJI.get(plan, '?')} *{plan_label(plan)}*  ({acc['days_left']} days)\n\n"
            f"Choose a pair and tap ▶️ Analyse & Enter"
        )

    await update.message.reply_text(
        welcome,
        reply_markup=kb_main_for(cid, plan, DEFAULT_PAIR),
        parse_mode="Markdown",
    )


async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral link and stats."""
    cid = update.effective_chat.id
    db_upsert_user(cid)
    stats = db_referral_stats(cid)
    ref_link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{cid}"

    bars = "🟢" * min(stats["bonused"], 10)
    text = (
        f"🤝 *Refer a Friend*\n"
        f"{'─' * 28}\n\n"
        f"For every friend who joins using your link:\n"
        f"*+{REFERRAL_BONUS_DAYS} free days* added to your plan automatically!\n\n"
        f"*Your referral link:*\n"
        f"`{ref_link}`\n\n"
        f"{'─' * 28}\n"
        f"📊 *Your stats:*\n"
        f"👥 Friends invited: *{stats['total']}*\n"
        f"✅ Bonuses earned: *{stats['bonused']}* {bars}\n"
        f"⏳ Pending: *{stats['pending']}*\n"
        f"🎁 Total days earned: *{stats['days_earned']}*\n\n"
        f"💡 _Share on social media, send to trading groups,\n"
        f"or ask your friend to forward it!_"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Share link", switch_inline_query=ref_link),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ADMIN_ID:
        return
    s    = db_stats()
    utm  = db_utm_stats()
    utm_lines = "\n".join(f"  {src}: {cnt}" for src, cnt in utm.items()) or "  no data"
    await update.message.reply_text(
        f"👑 *Admin Panel*\n\n"
        f"👥 Users: {s['total']}\n🔬 Trial: {s['trial']}\n"
        f"⭐ Basic: {s['basic']}\n💎 Pro: {s['pro']}\n💠 Diamond: {s['diamond']}\n❌ Expired: {s['expired']}\n\n"
        f"📨 Posts: {s['posts']}\n⭐ Stars: {s['total_stars']}\n\n"
        f"📡 *Traffic sources:*\n{utm_lines}",
        parse_mode="Markdown",
    )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /give <chat_id> <plan> <months>"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("❌ Format: /give 123456 pro 1")
        return
    try:
        cid    = int(args[0])
        pk     = args[1].lower()
        months = int(args[2])
        assert pk in ("basic", "pro", "diamond", "trial"), f"Unknown plan: {pk}"
        new_exp = db_apply_payment(cid, 0, pk, months, "manual")
        await update.message.reply_text(
            f"✅ *{pk}* until {new_exp.strftime('%d.%m.%Y')} for {cid}",
            parse_mode="Markdown",
        )
    except (ValueError, AssertionError) as e:
        await update.message.reply_text(f"❌ {e}\nFormat: /give 123456 pro 1")


async def cmd_forcepost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /forcepost [XAUUSD|BTCUSD|ETHUSD] [post_type]"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args      = context.args or []
    pair      = args[0].upper() if args and args[0].upper() in PAIRS else DEFAULT_PAIR
    post_type = args[1] if len(args) > 1 else "manual"
    cfg       = PAIRS[pair]

    await update.message.reply_text(f"📤 Publishing {cfg['emoji']} {cfg['name']}…")
    price = get_price(pair)
    if not price:
        await update.message.reply_text("❌ Could not get price.")
        return

    a    = await asyncio.to_thread(
        full_analysis, price, _prev_prices.get(pair), pair, GROQ_MODEL_NEWS,
    )
    text = groq_channel_post(a, post_type)
    try:
        sent = await safe_send_photo(context.bot, CHANNEL_ID, cfg["image"], text)
        if not sent:
            await safe_send(context.bot, CHANNEL_ID, text)
        db_save_post(pair, post_type, a["score"], a["ai"].get("sentiment", "?"), price, 0)
        await update.message.reply_text(f"✅ Published! Score={a['score']}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ═══════════════════════════════════════════════════════════════════
#  Deep Analysis  (Gemini or OpenRouter — see DEEP_ANALYSIS_PROVIDER)
# ═══════════════════════════════════════════════════════════════════

def _get_multi_tf_data(pair: str) -> dict:
    """
    Fetch OHLCV data on 4 timeframes: 5m, 15m, 1h, 4h.
    Returns dict with technicals per timeframe.
    """
    import yfinance as yf
    import pandas as pd

    ticker = PAIRS[pair]["yahoo"]
    cfg    = PAIRS[pair]

    tf_map = {
        "5m":  ("2d",  "5m"),
        "15m": ("5d",  "15m"),
        "1h":  ("30d", "1h"),
        "4h":  ("60d", "1h"),   # yfinance has no 4h — use 1h and resample
    }

    result = {}

    for label, (period, interval) in tf_map.items():
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 10:
                continue
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)

            # Resample to 4h
            if label == "4h":
                df = df.resample("4h").agg({
                    "Open":   "first", "High": "max",
                    "Low":    "min",   "Close": "last",
                    "Volume": "sum",
                }).dropna()

            if len(df) < 10:
                continue

            closes = df["Close"].squeeze().values.flatten().astype(float)
            highs  = df["High"].squeeze().values.flatten().astype(float)
            lows   = df["Low"].squeeze().values.flatten().astype(float)

            # EMA
            ema20 = float(pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1])
            ema200= float(pd.Series(closes).ewm(span=200, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else 0

            # RSI — fixed
            delta = pd.Series(closes).diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean() + 1e-9
            rsi_series = 100 - 100 / (1 + gain / loss)
            rsi   = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0

            # MACD
            ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean()
            ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean()
            macd  = float((ema12 - ema26).iloc[-1])
            signal= float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])

            # Support / Resistance — last 50 candles
            recent_h = highs[-50:] if len(highs) >= 50 else highs
            recent_l = lows[-50:]  if len(lows)  >= 50 else lows
            resist   = round(float(pd.Series(recent_h).nlargest(3).mean()),  2)
            support  = round(float(pd.Series(recent_l).nsmallest(3).mean()), 2)

            # Pivot points (classic)
            prev_h = float(highs[-2]) if len(highs) >= 2 else float(highs[-1])
            prev_l = float(lows[-2])  if len(lows)  >= 2 else float(lows[-1])
            prev_c = float(closes[-2]) if len(closes) >= 2 else float(closes[-1])
            pivot  = round((prev_h + prev_l + prev_c) / 3, 2)
            r1     = round(2 * pivot - prev_l, 2)
            s1     = round(2 * pivot - prev_h, 2)
            r2     = round(pivot + (prev_h - prev_l), 2)
            s2     = round(pivot - (prev_h - prev_l), 2)

            current = round(float(closes[-1]), 2)
            result[label] = {
                "current": current,
                "ema20":   round(ema20, 2),
                "ema50":   round(ema50, 2),
                "ema200":  round(ema200, 2) if ema200 else None,
                "rsi":     round(rsi, 1),
                "macd":    round(macd, 4),
                "macd_signal": round(signal, 4),
                "macd_cross": "bullish" if macd > signal else "bearish",
                "trend":   "up" if current > ema50 else "down",
                "support": support,
                "resist":  resist,
                "pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2,
                "candles": len(df),
            }
        except Exception as e:
            log.warning("Multi-TF error (%s %s): %s", label, pair, e)

    return result


def _get_macro_context(pair: str) -> str:
    """
    Fetch macro context: DXY trend, recent gold/Fed/inflation news.
    Gold is heavily correlated with DXY, real yields, and risk sentiment.
    """
    queries = [
        "gold XAU USD price forecast",
        "Federal Reserve interest rates inflation",
        "DXY dollar index trend",
        "gold geopolitical risk safe haven",
    ]
    results = []
    for q in queries:
        try:
            r = requests.get(
                f"https://newsapi.org/v2/everything"
                f"?q={q.replace(' ', '%20')}&sortBy=publishedAt"
                f"&pageSize=3&language=en&apiKey={NEWS_API}",
                timeout=5,
            )
            r.raise_for_status()
            for a in r.json().get("articles", [])[:2]:
                title = a.get("title", "")
                desc  = (a.get("description") or "")[:100]
                if title:
                    results.append(f"• {title}. {desc}")
        except Exception:
            pass
    return "\n".join(results[:8]) if results else "No macro news available"


def _build_deep_prompt(pair: str, price: float, tf_data: dict,
                       macro: str, econ: dict) -> str:
    """Build the comprehensive prompt for the deep-analysis LLM."""
    cfg = PAIRS[pair]

    # Format multi-timeframe data
    tf_text = ""
    for tf, d in tf_data.items():
        tf_text += (
            f"\n[{tf.upper()}] Price={d['current']} | "
            f"EMA20={d['ema20']} EMA50={d['ema50']}"
            + (f" EMA200={d['ema200']}" if d.get("ema200") else "") +
            f" | RSI={d['rsi']} | MACD={d['macd_cross'].upper()} | "
            f"Trend={d['trend'].upper()}\n"
            f"  Support={d['support']} | Resistance={d['resist']}\n"
            f"  Pivot={d['pivot']} R1={d['r1']} R2={d['r2']} "
            f"S1={d['s1']} S2={d['s2']}"
        )

    econ_text = ""
    if econ.get("has_danger"):
        econ_text = f"\n⚠️ HIGH-IMPACT EVENTS TODAY: {', '.join(econ['events'])}"

    return f"""You are a senior proprietary trader and technical analyst with 15+ years experience in {cfg['name']}.
You have access to real multi-timeframe data. Your task is to produce an IMMEDIATELY ACTIONABLE trading report.

═══ LIVE MARKET DATA ═══
Asset: {cfg['name']}
Current Price: {fmt_price(price, pair)}
{econ_text}

═══ MULTI-TIMEFRAME TECHNICALS ═══
{tf_text}

═══ MACRO & NEWS ═══
{macro}

═══ REQUIRED OUTPUT FORMAT ═══
Respond ONLY in this exact structure. Use the actual numbers from the data above.

1. MARKET BIAS
   Direction: [BULLISH / BEARISH / NEUTRAL]
   Confidence: [X]%
   Reason: (2-3 sentences using the TF data above)

2. TIMEFRAME ALIGNMENT
   5m : [trend] | RSI=[value] | MACD=[value]
   15m: [trend] | RSI=[value] | MACD=[value]
   1h : [trend] | RSI=[value] | MACD=[value]
   4h : [trend] | RSI=[value] | MACD=[value]
   Alignment: [ALIGNED / MIXED / CONFLICTING] — reason

3. KEY PRICE LEVELS
   Resistance 1: [exact price] — reason
   Resistance 2: [exact price] — reason
   Resistance 3: [exact price] — reason
   Support 1:    [exact price] — reason
   Support 2:    [exact price] — reason
   Support 3:    [exact price] — reason
   🔑 Most critical level RIGHT NOW: [price] because [reason]

4. TRADE SETUP A (primary)
   Direction : BUY / SELL
   Entry zone: [exact price or range, e.g. 3285–3290]
   Stop Loss : [exact price] (reason: [why this level])
   TP1       : [exact price] (+[R] R) — conservative
   TP2       : [exact price] (+[R] R) — main target
   TP3       : [exact price] (+[R] R) — extended
   R:R ratio : [X:1] (to TP2)
   Trigger   : [what must happen to enter — e.g. "break and close above 3295 on 15m"]
   Timeframe : [expected duration]

5. TRADE SETUP B (alternative / counter-trend)
   Direction : BUY / SELL
   Entry zone: [exact price or range]
   Stop Loss : [exact price]
   TP1       : [exact price]
   TP2       : [exact price]
   TP3       : [exact price]
   R:R ratio : [X:1]
   Trigger   : [entry condition]
   Timeframe : [expected duration]

6. MACRO CONTEXT
   News impact : [how top news affects this asset right now]
   DXY / macro : [dollar / risk sentiment effect]
   Watch next 24h: [key event or level to monitor]

7. INVALIDATION
   Bullish scenario fails if: [exact price level]
   Bearish scenario fails if: [exact price level]

8. FINAL VERDICT
   Best setup: [A or B]
   Score     : [X]/100
   Action NOW: [ENTER / WAIT FOR TRIGGER / AVOID]
   Entry timing: [immediate / on pullback to X / on breakout above X]
   Risk per trade: [suggested % of capital, e.g. 1–2%]

Be precise. Use exact prices from the data. No vague statements."""


def _resolved_deep_provider() -> str:
    """Which backend runs /deepanalysis: gemini | openrouter | none (auto picks first available)."""
    p = (DEEP_ANALYSIS_PROVIDER or "gemini").strip().lower()
    if p not in ("gemini", "openrouter", "auto"):
        p = "gemini"
    if p == "openrouter":
        return "openrouter"
    if p == "gemini":
        return "gemini"
    if GEMINI_KEY:
        return "gemini"
    if _openrouter_configured():
        return "openrouter"
    return "none"


def _deep_analysis_model_label() -> str:
    return OPENROUTER_MODEL if _resolved_deep_provider() == "openrouter" else GEMINI_MODEL


def _deep_analysis_config_ok() -> tuple[bool, str]:
    w = _resolved_deep_provider()
    if w == "none":
        return False, (
            "No AI backend for deep analysis. Set `GEMINI_KEY` and/or OpenRouter keys "
            "(`OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_2`), "
            "or adjust `DEEP_ANALYSIS_PROVIDER` (gemini | openrouter | auto)."
        )
    if w == "openrouter" and not _openrouter_configured():
        return (
            False,
            "`DEEP_ANALYSIS_PROVIDER` uses OpenRouter but no OpenRouter keys are set "
            "(set `OPENROUTER_API_KEY` and optionally `OPENROUTER_API_KEY_2`).",
        )
    if w == "gemini" and not GEMINI_KEY:
        return False, "`DEEP_ANALYSIS_PROVIDER` uses Gemini but `GEMINI_KEY` is missing."
    return True, ""


def _resolved_chart_provider() -> str:
    p = (CHART_VISION_PROVIDER or "gemini").strip().lower()
    if p not in ("gemini", "openrouter", "auto"):
        p = "gemini"
    if p == "openrouter":
        return "openrouter"
    if p == "gemini":
        return "gemini"
    if GEMINI_KEY:
        return "gemini"
    if _openrouter_configured():
        return "openrouter"
    return "none"


def _chart_vision_config_ok() -> tuple[bool, str]:
    w = _resolved_chart_provider()
    if w == "none":
        return False, (
            "Set `GEMINI_KEY` or OpenRouter keys (`OPENROUTER_API_KEY` / `OPENROUTER_API_KEY_2`) "
            "for chart vision (or `CHART_VISION_PROVIDER`)."
        )
    if w == "openrouter" and not _openrouter_configured():
        return (
            False,
            "Chart vision uses OpenRouter but no OpenRouter keys are configured.",
        )
    if w == "gemini" and not GEMINI_KEY:
        return False, "Chart vision uses Gemini but `GEMINI_KEY` is missing."
    return True, ""


def _chart_model_label() -> str:
    if _resolved_chart_provider() == "openrouter":
        return _openrouter_vision_model()
    return GEMINI_MODEL


def _openrouter_deep_analysis(pair: str, price: float) -> str:
    """Run deep analysis using OpenRouter (long-form report)."""
    # Gather all data
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_tf    = pool.submit(_get_multi_tf_data, pair)
        f_macro = pool.submit(_get_macro_context, pair)
        f_econ  = pool.submit(_check_econ_calendar)
        try:
            tf_data = f_tf.result(timeout=25)
        except Exception as e:
            log.warning("Multi-TF timeout: %s", e)
            tf_data = {}
        try:
            macro = f_macro.result(timeout=10)
        except Exception:
            macro = ""
        try:
            econ = f_econ.result(timeout=6)
        except Exception:
            econ = {"has_danger": False, "events": []}

    prompt = _build_deep_prompt(pair, price, tf_data, macro, econ)

    return _openrouter_chat(
        [{"role": "user", "content": prompt}],
        max_tokens=1800,
        temperature=0.35,
    )


def _gemini_deep_analysis(pair: str, price: float) -> str:
    """Run deep analysis using Google Gemini (long-form report)."""
    import google.genai as genai
    import google.genai.types as gtypes

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_tf    = pool.submit(_get_multi_tf_data, pair)
        f_macro = pool.submit(_get_macro_context, pair)
        f_econ  = pool.submit(_check_econ_calendar)
        try:
            tf_data = f_tf.result(timeout=25)
        except Exception as e:
            log.warning("Multi-TF timeout: %s", e)
            tf_data = {}
        try:
            macro = f_macro.result(timeout=10)
        except Exception:
            macro = ""
        try:
            econ = f_econ.result(timeout=6)
        except Exception:
            econ = {"has_danger": False, "events": []}

    prompt = _build_deep_prompt(pair, price, tf_data, macro, econ)

    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            max_output_tokens=1800,
        ),
    )
    return response.text


def _deep_analysis_llm_call(pair: str, price: float) -> str:
    w = _resolved_deep_provider()
    if w == "none":
        raise RuntimeError("No deep-analysis backend configured")
    if w == "openrouter":
        return _openrouter_deep_analysis(pair, price)
    return _gemini_deep_analysis(pair, price)


async def cmd_deepanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /deepanalysis          — shows pair selection
      /deepanalysis BTCUSD   — analyse specific pair directly
    Trial: 1/day (XAU only). Diamond: 3/day (all pairs). Admin: unlimited.
    """
    cid = update.effective_chat.id
    acc = db_access(cid)
    plan = acc["plan"]

    if plan not in ("trial", "diamond", "admin") and cid != ADMIN_ID:
        await update.message.reply_text(
            "💠 *Deep Analysis* is available on Trial (1/day) and Diamond (3/day).\n\n"
            "Tap /start → 💳 Subscription",
            parse_mode="Markdown",
        )
        return

    ok, err = _deep_analysis_config_ok()
    if not ok:
        await update.message.reply_text(f"❌ {err}", parse_mode="Markdown")
        return

    # Check daily limit
    if cid != ADMIN_ID:
        used = db_deepanalysis_count_today(cid)
        limit = 1 if plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
        if used >= limit:
            if plan == "trial":
                await update.message.reply_text(
                    "⏳ *Trial limit reached* (1/day)\n\n"
                    "Upgrade to 💠 Diamond to get *3 deep analyses per day* on any pair.\n\n"
                    "Tap /start → 💳 Subscription",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"⏳ *Daily limit reached* ({limit}/day)\n\n"
                    f"You've used all {limit} deep analyses for today.\n"
                    f"Resets at midnight UTC.",
                    parse_mode="Markdown",
                )
            return

    args = context.args or []

    # If pair provided as argument — run directly
    direct_pair = None
    for arg in args:
        if arg.upper() in PAIRS:
            direct_pair = arg.upper()
            break

    if direct_pair:
        await _run_deepanalysis(update, context, cid, acc, direct_pair)
        return

    # Otherwise show pair selection keyboard
    plan = acc["plan"]
    rows = []
    for pid, cfg in PAIRS.items():
        if plan in cfg["plans"] or cid == ADMIN_ID:
            rows.append([InlineKeyboardButton(
                f"{cfg['emoji']} {cfg['name']}",
                callback_data=f"deepanalysis_{pid}",
            )])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="deepanalysis_cancel")])

    used = db_deepanalysis_count_today(cid) if cid != ADMIN_ID else 0
    limit = 1 if plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
    if cid != ADMIN_ID:
        remaining = f"  ({limit - used} left today)"
    else:
        remaining = ""

    await update.message.reply_text(
        f"🧠 *Deep Analysis{remaining}*\n\nChoose a pair to analyse:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _run_deepanalysis(update_or_query, context, cid: int, acc: dict, pair: str) -> None:
    """Execute deep analysis for the given pair and user."""
    cfg = PAIRS[pair]

    async def reply(text, **kwargs):
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(text, **kwargs)
        else:
            await context.bot.send_message(cid, text, **kwargs)

    await reply(
        f"🧠 *Deep Analysis* — {cfg['emoji']} {cfg['name']}\n\n"
        f"Model: `{_deep_analysis_model_label()}`\n"
        f"⏳ Gathering data from 4 timeframes + macro news…\n\n"
        f"_This takes 30-60 seconds — please wait_",
        parse_mode="Markdown",
    )

    price = get_price(pair)
    if not price:
        await reply("❌ Could not get current price.")
        return

    try:
        loop   = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _deep_analysis_llm_call, pair, price),
            timeout=120,
        )
    except asyncio.TimeoutError:
        await reply("⏱ Analysis timed out (120s). Please try again.", parse_mode="Markdown")
        return
    except Exception as e:
        await reply(f"❌ Error: {str(e)[:200]}")
        log.error("Deep analysis error: %s", e)
        return

    # Log usage (admin unlimited)
    if cid != ADMIN_ID:
        db_deepanalysis_log(cid, pair)
        used  = db_deepanalysis_count_today(cid)
        limit = 1 if acc["plan"] == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
        remaining_note = f"\n_Deep analyses today: {used}/{limit}_"
        if acc["plan"] == "trial" and used >= limit:
            remaining_note += "\n_Upgrade to 💠 Diamond for 3/day on all pairs_"
    else:
        remaining_note = ""

    # Split long messages (Telegram limit 4096 chars)
    header = (
        f"🧠 *DEEP ANALYSIS — {cfg['emoji']} {cfg['name']}*\n"
        f"💰 Price: *{fmt_price(price, pair)}*  |  "
        f"Model: `{_deep_analysis_model_label()}`\n"
        f"{'─' * 30}\n\n"
    )
    full_text = header + result + remaining_note

    chunk_size = 3800
    chunks = []
    while len(full_text) > chunk_size:
        split_at = full_text.rfind("\n\n", 0, chunk_size)
        if split_at == -1:
            split_at = chunk_size
        chunks.append(full_text[:split_at])
        full_text = full_text[split_at:].lstrip()
    if full_text:
        chunks.append(full_text)

    for i, chunk in enumerate(chunks):
        try:
            await reply(chunk, parse_mode="Markdown")
        except Exception:
            plain = re.sub(r"[*_`#]", "", chunk)
            await reply(plain)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)

    log.info("Deep analysis: cid=%s %s model=%s price=%s", cid, pair, _deep_analysis_model_label(), price)


# ═══════════════════════════════════════════════════════════════════
#  Vision Chart Analysis  (Gemini or OpenRouter — see CHART_VISION_PROVIDER)
# ═══════════════════════════════════════════════════════════════════

async def cmd_chartanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User sends a chart screenshot → Gemini or OpenRouter (multimodal).
    Usage: send photo with caption /chart or just /chart then send photo
    Available to Diamond plan users only.
    """
    cid = update.effective_chat.id
    acc = db_access(cid)
    if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
        await update.message.reply_text(
            "💠 *Chart AI Analysis* is a Diamond-exclusive feature.\n\n"
            "Upgrade to Diamond to unlock:\n"
            "• 📸 Screenshot chart analysis\n"
            "• Priority auto-signals\n"
            "• Lower alert threshold\n\n"
            "Tap /start → 💳 Subscription",
            parse_mode="Markdown",
        )
        return

    # Check if message has a photo or image document
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]   # largest size
    elif update.message.document and update.message.document.mime_type and \
            update.message.document.mime_type.startswith("image/"):
        photo = update.message.document
    elif update.message.reply_to_message:
        rm = update.message.reply_to_message
        if rm.photo:
            photo = rm.photo[-1]
        elif rm.document and rm.document.mime_type and \
                rm.document.mime_type.startswith("image/"):
            photo = rm.document

    if not photo:
        await update.message.reply_text(
            "📸 *How to use Chart Analysis:*\n\n"
            "1. Open your broker/TradingView chart\n"
            "2. Set your timeframe and indicators\n"
            "3. Take a screenshot\n"
            "4. Send the screenshot to this bot\n"
            "   _(caption is optional)_\n\n"
            "The AI will analyse the chart and give you:\n"
            "• Trend direction and strength\n"
            "• Key support & resistance levels\n"
            "• Entry, SL and TP suggestion\n"
            "• Overall trade recommendation",
            parse_mode="Markdown",
        )
        return

    ok, err = _chart_vision_config_ok()
    if not ok:
        await update.message.reply_text(f"❌ {err}", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "🔍 *Analysing your chart…*\n"
        f"_Model `{_chart_model_label()}` — about 15–45 seconds_",
        parse_mode="Markdown",
    )

    try:
        # Download photo from Telegram
        photo_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await photo_file.download_as_bytearray()

        # User caption as additional context
        user_note = ""
        if update.message.caption and update.message.caption.strip():
            note = update.message.caption.replace("/chart", "").strip()
            if note:
                user_note = f"\nUser note: {note}"

        prompt = (
            "You are a trading chart analyst. Analyse this chart screenshot.\n"
            "Identify the asset and timeframe from the chart itself.\n"
            f"{user_note}\n\n"
            "Give a short, clear analysis:\n\n"
            "Asset: [pair] | Timeframe: [TF]\n\n"
            "Trend: UP / DOWN / SIDEWAYS — [1 sentence why]\n\n"
            "Key levels:\n"
            "  Resistance: [price]\n"
            "  Support: [price]\n\n"
            "Signal:\n"
            "  Direction: BUY / SELL\n"
            "  Entry: [price or zone]\n"
            "  SL: [price]\n"
            "  TP1: [price]\n"
            "  TP2: [price]\n"
            "  TP3: [price]\n\n"
            "Verdict: ENTER / WAIT / AVOID — [1 sentence]\n\n"
            "Keep it concise. Use prices visible on the chart."
        )

        if _resolved_chart_provider() == "openrouter":
            mime = getattr(photo, "mime_type", None) or "image/jpeg"
            b64 = base64.standard_b64encode(bytes(photo_bytes)).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
            result = _openrouter_chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                model=_openrouter_vision_model(),
                max_tokens=1000,
                temperature=0.3,
            )
        else:
            import google.genai as genai
            import google.genai.types as gtypes
            import PIL.Image

            client = genai.Client(api_key=GEMINI_KEY)
            image = PIL.Image.open(io.BytesIO(bytes(photo_bytes)))
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, image],
                config=gtypes.GenerateContentConfig(
                    max_output_tokens=1000,
                ),
            )
            result = response.text

        # Strip common markdown so plain-text Telegram replies stay readable
        result = re.sub(r"\*+", "", result)
        result = re.sub(r"_+",  "", result)
        result = re.sub(r"`+",  "", result)
        result = re.sub(r"#+\s*", "", result)

        header = "📊 Chart Analysis\n" + "─" * 28 + "\n\n"
        full   = header + result

        # Split at paragraph boundaries
        parts = []
        remaining = full
        while len(remaining) > 3800:
            split_at = remaining.rfind("\n\n", 0, 3800)
            if split_at == -1:
                split_at = remaining.rfind("\n", 0, 3800)
            if split_at == -1:
                split_at = 3800
            parts.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()
        if remaining:
            parts.append(remaining)

        for i, part in enumerate(parts):
            await update.message.reply_text(part)
            if i < len(parts) - 1:
                await asyncio.sleep(0.5)

        log.info("Chart analysis: cid=%s plan=%s model=%s", cid, acc["plan"], _chart_model_label())

    except Exception as e:
        await update.message.reply_text(
            f"❌ Analysis failed: {str(e)[:150]}\n\nTry again in a moment."
        )
        log.error("Chart analysis error: %s", e)


async def handle_photo(update, context):
    cid = update.effective_chat.id
    acc = db_access(cid)
    if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
        return
    if update.message and (update.message.photo or update.message.document):
        await cmd_chartanalysis(update, context)

async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ADMIN_ID:
        return
    await update.message.reply_text("Preparing welcome post…")
    prices = {p: (fmt_price(v, p) if (v := get_price(p)) else "N/A") for p in PAIRS}
    xau = prices["XAUUSD"]; btc = prices["BTCUSD"]; eth = prices["ETHUSD"]
    sol = prices.get("SOLUSD", "N/A"); xrp = prices.get("XRPUSD", "N/A")
    text = (
        "<b>📊 Gold &amp; Crypto AI Signals</b>\n"
        "<i>AI-powered signals — 8 pairs</i>\n\n"
        "📡 AI market analysis 3x per day:\n"
        "☀️ 09:00  📊 15:00  🌙 21:00 (Kyiv time)\n\n"
        "<b>── Current prices ──</b>\n"
        f"🥇 XAU/USD  <code>{xau}</code>\n"
        f"🥈 XAG/USD  <code>{prices.get(chr(39)+chr(88)+chr(65)+chr(71)+chr(85)+chr(83)+chr(68)+chr(39), chr(78)+chr(47)+chr(65))}</code>\n"
        f"₿  BTC/USD  <code>{btc}</code>\n"
        f"Ξ  ETH/USD  <code>{eth}</code>\n"
        f"◎  SOL/USD  <code>{sol}</code>\n"
        f"✕  XRP/USD  <code>{xrp}</code>\n"
        f"🔶 BNB/USD  <code>{prices.get(chr(66)+chr(78)+chr(66)+chr(85)+chr(83)+chr(68), chr(78)+chr(47)+chr(65))}</code>\n"
        f"🔵 ADA/USD  <code>{prices.get(chr(65)+chr(68)+chr(65)+chr(85)+chr(83)+chr(68), chr(78)+chr(47)+chr(65))}</code>\n\n"
        "<b>── Plans ──</b>\n\n"
        "⭐ <b>Basic</b> — $5/mo\n"
        "XAU/USD signals + SL/TP alerts\n"
        "3 months — $12.5 🔥 (save 17%)\n\n"
        "💎 <b>Pro</b> — $9.99/mo\n"
        "XAU + BTC + ETH + XAG\n"
        "Auto-signals 24/7\n"
        "3 months — $25 🔥 (save 17%)\n\n"
        "👑 <b>Diamond</b> — $19.99/mo\n"
        "ALL 8 pairs (XAU XAG BTC ETH SOL XRP BNB ADA)\n"
        "Chart screenshot analysis\n"
        "Deep AI analysis\n"
        "Priority signals\n"
        "3 months — $49.99 🔥 (save 17%)\n\n"
        "🎁 <b>First week FREE</b>\n\n"
        f"👇 Start → {BOT_USERNAME}"
    )
    try:
        msg = await context.bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        try:
            await context.bot.pin_chat_message(CHANNEL_ID, msg.message_id,
                                               disable_notification=True)
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ Welcome post published and pinned!\nXAU={xau} · BTC={btc} · ETH={eth}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ═══════════════════════════════════════════════════════════════════
#  Button handler
# ═══════════════════════════════════════════════════════════════════

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await safe_callback_answer(q)

    cid  = q.message.chat_id
    u    = get_user(cid)
    acc  = db_access(cid)
    plan = acc["plan"]

    # Deep analysis pair selection
    if q.data == "deepanalysis_cancel":
        await q.message.delete()
        return

    if q.data == "chart_ai":
        if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
            await safe_edit(
                q,
                "💠 *Chart AI* is available on the *Diamond* plan only.\n\n"
                "Upgrade in *Subscription* to unlock chart screenshot analysis.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_main")]]),
            )
            return
        await safe_edit(q,
            "📸 *Chart AI Analysis*\n\n"
            "Send me a screenshot of your chart and I'll analyse it.\n\n"
            "1. Take a screenshot of your TradingView/broker chart\n"
            "2. Send the photo to this chat\n"
            "   _(caption is optional)_",
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_main")]]),
        )
        return

    if q.data == "deepanalysis_menu":
        cur_plan = acc["plan"]
        if cur_plan not in ("trial", "diamond", "admin") and cid != ADMIN_ID:
            await safe_edit(
                q,
                "🔒 *Deep Analysis* is not available on your current plan.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_main")]]),
            )
            return
        if cid != ADMIN_ID:
            used = db_deepanalysis_count_today(cid)
            limit = 1 if cur_plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
            if used >= limit:
                msg = (f"⏳ *Trial limit reached* (1/day).\nUpgrade to Diamond for 3/day."
                       if cur_plan == "trial"
                       else f"⏳ *Daily limit reached* ({limit}/day). Resets at midnight UTC.")
                await safe_edit(
                    q,
                    msg,
                    markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_main")]]),
                )
                return
            remaining = f"  ({limit - used} left today)"
        else:
            remaining = ""
        rows = []
        for pid, cfg in PAIRS.items():
            if cur_plan in cfg["plans"] or cid == ADMIN_ID:
                rows.append([InlineKeyboardButton(
                    f"{cfg['emoji']} {cfg['name']}",
                    callback_data=f"deepanalysis_{pid}",
                )])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="deepanalysis_cancel")])
        await safe_edit(q, f"🧠 *Deep Analysis{remaining}*\n\nChoose a pair to analyse:", markup=InlineKeyboardMarkup(rows))
        return

    if q.data.startswith("deepanalysis_"):
        pair_key = q.data[len("deepanalysis_"):]
        if pair_key not in PAIRS:
            return
        await q.message.delete()
        await _run_deepanalysis(update, context, cid, acc, pair_key)
        return

    if q.data == "choose_pair":
        await safe_edit(q, "🔀 *Select pair*\n\n🔒 XAG — Basic+\n🔒 BTC/ETH/SOL/XRP/BNB/ADA — Pro+",
                        markup=kb_pairs(u.selected_pair, plan))
        return

    if q.data.startswith("pair_"):
        new_pair = q.data[5:]
        cfg = PAIRS.get(new_pair)
        if not cfg:
            await safe_edit(q, "❌ Unknown pair.", markup=kb_main_for(cid, plan, u.selected_pair))
            return
        if plan not in cfg["plans"]:
            await safe_edit(q, f"🔒 *{cfg['name']}* requires Pro or Diamond plan.", markup=kb_sub())
            return
        u.selected_pair = new_pair
        price = get_price(new_pair)
        await safe_edit(
            q,
            f"✅ *{cfg['emoji']} {cfg['name']}*\n\n"
            f"Price: *{fmt_price(price, new_pair) if price else 'N/A'}*",
            markup=kb_main_for(cid, plan, new_pair),
        )
        return

    if q.data == "back_main":
        await safe_edit(q, "🤖 *AI Trading Bot*", markup=kb_main_for(cid, plan, u.selected_pair))
        return

    if q.data == "refer":
        stats    = db_referral_stats(cid)
        ref_link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{cid}"
        bars     = "🟢" * min(stats["bonused"], 10)
        text = (
            f"🤝 *Refer a Friend — Earn Free Days*\n"
            f"{'─' * 28}\n\n"
            f"Share your link → friend joins → you get *+{REFERRAL_BONUS_DAYS} free days!*\n\n"
            f"*Your link:*\n`{ref_link}`\n\n"
            f"{'─' * 28}\n"
            f"👥 Invited: *{stats['total']}*\n"
            f"✅ Bonuses: *{stats['bonused']}* {bars}\n"
            f"🎁 Days earned: *{stats['days_earned']}*"
        )
        await safe_edit(q, text, markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Back", callback_data="back_main")],
        ]))
        return

    if q.data == "sub_menu":
        await safe_edit(q, sub_info_text(acc), markup=kb_sub())
        return

    # ── Crypto payments (NOWPayments) ────────────────────────────
    if q.data == "crypto_menu":
        if not (NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL and NOWPAYMENTS_IPN_SECRET):
            await safe_edit(
                q,
                "❌ Crypto payments are not configured.\n\n"
                "Admin needs to set:\n"
                "- NOWPAYMENTS_API_KEY\n"
                "- NOWPAYMENTS_IPN_SECRET\n"
                "- PUBLIC_BASE_URL",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="sub_menu")]]),
            )
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("₮ Basic — 3 mo ($12.5)", callback_data="crypto_pay_basic_3")],
            [InlineKeyboardButton("₮ Pro — 3 mo ($25)", callback_data="crypto_pay_pro_3")],
            [InlineKeyboardButton("₮ Diamond — 1 mo ($19.99)", callback_data="crypto_pay_diamond_1")],
            [InlineKeyboardButton("₮ Diamond — 3 mo ($49.99)", callback_data="crypto_pay_diamond_3")],
            [InlineKeyboardButton("↩️ Back", callback_data="sub_menu")],
        ])
        crypto_menu_intro = (
            "₮ *Pay with Crypto (USDT TRC20)*\n\n"
            "*UA:* Обери план — отримаєш адресу й точну суму. Доступ активується після підтвердження в мережі.\n"
            "*EN:* Pick a plan — you get address + exact amount; access unlocks once the network confirms.\n\n"
            "⚠️ *Чому немає Basic/Pro на 1 міс криптою?*\n"
            "Платіжний посередник *NOWPayments* і правила мережі задають мінімальну суму платежу. "
            "Найдешевші місячні плани через криптомережу часто просто технічно не проходять без великої несправедливої доплати "
            "(це не наш довільний барʼєр, а умови процесинг-партнера). "
            "Тому *Basic та Pro ми пропонуємо криптою пакунком на 3 місяці*, а нижчі 1 міс — також через "
            "*⭐ Telegram Stars* головному меню підписки.\n\n"
            "*Diamond* можна платити криптою і на *1 міс*, і на *3 міс*.\n\n"
            "📋 Обери варіант нижче."
        )
        await safe_edit(
            q,
            crypto_menu_intro,
            markup=kb,
        )
        return

    if q.data.startswith("crypto_pay_"):
        if not (NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL and NOWPAYMENTS_IPN_SECRET):
            await safe_edit(
                q,
                "❌ *Crypto payments* are not configured on this bot.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="sub_menu")]]),
            )
            return
        try:
            _, _, plan_key, months_s = q.data.split("_", 3)
            months = int(months_s)
        except Exception:
            await safe_edit(
                q,
                "❌ Bad crypto plan option.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
            return

        if (plan_key, months) not in CRYPTO_PAY_ALLOWED:
            denied = (
                "⚠️ *Цей план недоступний криптою*\n\n"
                "Платіжний посередник *NOWPayments* і мінімуми мережі встановлюють мінімальну суму платежу. "
                "Короткі дешеві періоди *Basic / Pro на 1 міс* криптом зазвичай не створюються без великої "
                "необгрунтованої надбавки — це *вимоги партнера-еквайєра*, не «забаганка» сервісу.\n\n"
                "*Що робити:*\n"
                "• оплатити ⭐ через *Subscription*\n"
                "• або обрати *Basic / Pro на 3 місяці* криптою в попередньому меню\n"
                "• *Diamond* — криптом і на *1 міс*, і на *3 міс* там само."
            )
            await safe_edit(
                q,
                denied,
                markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("↩️ Оплата криптою", callback_data="crypto_menu"),
                        InlineKeyboardButton("↩️ Підписка", callback_data="sub_menu"),
                    ],
                ]),
            )
            return

        price_map = {
            ("basic", 1): USD_BASIC_1,
            ("basic", 3): USD_BASIC_3,
            ("pro", 1): USD_PRO_1,
            ("pro", 3): USD_PRO_3,
            ("diamond", 1): USD_DIAMOND_1,
            ("diamond", 3): USD_DIAMOND_3,
        }
        price_usd = price_map.get((plan_key, months))
        if price_usd is None:
            await safe_edit(
                q,
                "❌ Unknown plan selection.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
            return

        await safe_edit(q, "⏳ Creating crypto invoice…")
        try:
            inv = nowp_create_payment(cid, plan_key, months, float(price_usd))
        except Exception as e:
            await safe_edit(
                q,
                f"❌ Could not create payment:\n{str(e)[:380]}",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
            return

        payment_id = inv["payment_id"]
        addr = inv.get("pay_address") or "—"
        amt = inv.get("pay_amount") or "—"
        url = inv.get("invoice_url")

        lines = [
            "₮ *Crypto Payment Created*",
            "",
            f"Plan: *{plan_label(plan_key)}*  |  Months: *{months}*",
        ]
        rq = inv.get("price_usd_requested")
        iq = inv.get("price_usd_invoiced")
        try:
            if rq is not None and iq is not None and round(float(iq) - float(rq), 4) >= 0.03:
                lines.append(
                    f"In USD (NOWPayments minimum): listed *${float(rq):.2f}* → invoiced *${float(iq):.2f}*"
                )
        except (TypeError, ValueError):
            pass

        lines += [
            f"Currency: *USDT (TRC20)*",
            f"Amount: *{amt}*",
            f"Address: `{addr}`",
            "",
            "After payment gets confirmed, your plan will activate automatically.",
        ]
        if url:
            lines += ["", f"Invoice link: `{url}`"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Check payment", callback_data=f"crypto_check_{payment_id}")],
            [InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")],
        ])
        await safe_edit(q, "\n".join(lines), markup=kb)
        return

    if q.data.startswith("crypto_check_"):
        payment_id = q.data[len("crypto_check_"):]
        row = db_crypto_payment_get(payment_id)
        if not row:
            await safe_edit(
                q,
                "❌ Payment not found or expired.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
            return
        await safe_edit(q, "⏳ Checking payment status…")
        try:
            st = nowp_get_payment(payment_id)
        except Exception as e:
            await safe_edit(
                q,
                f"❌ Status check failed: {str(e)[:160]}",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
            return
        status = str(st.get("payment_status") or st.get("status") or "waiting")
        db_crypto_payment_upsert(
            payment_id=payment_id,
            chat_id=int(row["chat_id"]),
            plan=str(row["plan"]),
            months=int(row["months"]),
            price_usd=float(row["price_usd"]),
            pay_currency=str(row["pay_currency"]),
            pay_amount=float(st.get("pay_amount")) if st.get("pay_amount") else row["pay_amount"],
            pay_address=str(st.get("pay_address") or row["pay_address"]),
            status=status,
        )
        if _nowp_is_paid(status):
            # activate if not already activated
            charge_id = f"nowp:{payment_id}"
            new_exp = db_apply_payment(int(row["chat_id"]), 0, str(row["plan"]), int(row["months"]), charge_id)
            await safe_edit(
                q,
                f"✅ *Payment confirmed!*\\n\\n"
                f"Plan: *{plan_label(str(row['plan']))}*\\n"
                f"Active until: *{new_exp.strftime('%d.%m.%Y')}*",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_main")]]),
            )
        elif _nowp_is_failed(status):
            await safe_edit(
                q,
                f"❌ Payment status: *{status}*\\n\\n"
                f"If you already paid, please wait for confirmations or try again.",
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
            )
        else:
            await safe_edit(
                q,
                f"⏳ Payment status: *{status}*\\n\\n"
                f"Waiting for confirmation…",
                markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"crypto_check_{payment_id}")],
                    [InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")],
                ]),
            )
        return

    buy_map = {
        "buy_basic_1":   ("basic",   1, PRICE_BASIC,     "Basic — 1 month"),
        "buy_basic_3":   ("basic",   3, PRICE_BASIC_3,   "Basic — 3 months"),
        "buy_pro_1":     ("pro",     1, PRICE_PRO,       "Pro — 1 month"),
        "buy_pro_3":     ("pro",     3, PRICE_PRO_3,     "Pro — 3 months"),
        "buy_diamond_1": ("diamond", 1, PRICE_DIAMOND,   "Diamond — 1 month"),
        "buy_diamond_3": ("diamond", 3, PRICE_DIAMOND_3, "Diamond — 3 months"),
    }
    if q.data in buy_map:
        pk, months, stars, title = buy_map[q.data]
        desc = (
            "XAU/USD analysis" if pk == "basic"
            else "XAU+BTC+ETH + auto-signals + chart AI" if pk == "diamond"
            else "XAU+BTC+ETH + auto-signals"
        )
        await context.bot.send_invoice(
            chat_id=cid, title=f"Trading Bot — {title}", description=desc,
            payload=f"{pk}_{months}", provider_token="",
            currency="XTR", prices=[LabeledPrice(title, stars)],
        )
        return

    if not acc["allowed"]:
        await safe_edit(q, "⛔ *Trial expired*", markup=kb_sub())
        return

    pair = u.selected_pair
    ps   = u.ps()
    cfg  = PAIRS[pair]

    if q.data in ("start", "refresh_analysis"):
        await safe_edit(q, f"⏳ *Fetching price {cfg['emoji']} {cfg['name']}…*")
        price_val = get_price(pair)
        if not price_val:
            await safe_edit(q, "❌ Could not get price.", markup=kb_main(plan, pair))
            return
        await safe_edit(q,
            f"🔄 *Analysing {cfg['emoji']} {cfg['name']}…*\n\n"
            f"💰 Price: *{fmt_price(price_val, pair)}*\n"
            f"_Fetching technicals, news and AI insight…_"
        )
        try:
            loop = asyncio.get_event_loop()
            a = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda pr=price_val, prev=_prev_prices.get(pair), pk=pair: full_analysis(
                        pr, prev, pk,
                    ),
                ),
                timeout=45,   # increased: parallel fetch ~15s + groq ~20s
            )
        except asyncio.TimeoutError:
            await safe_edit(q,
                "⏱ *Analysis timed out.*\n\n"
                "_The server took too long. This usually happens once — please try again._",
                markup=kb_main_for(cid, plan, pair),
            )
            return
        u.pending_analysis = a
        opt = a["ai"].get("optimal_entry", price_val)
        await safe_edit(
            q,
            build_analysis_text(a) + "\n\n*What would you like to do?*",
            markup=kb_confirm(opt, pair),
        )
        return

    if q.data == "confirm_now":
        price_val = get_price(pair)
        if not price_val:
            await safe_edit(q, "❌ Price error.", markup=kb_main_for(cid, plan, pair))
            return
        a  = u.pending_analysis or {}
        ai = a.get("ai", {})
        dr, de = _direction(ai, a.get("trend", "flat"), a.get("tech"))
        ps.entry_price     = price_val
        ps.running         = True
        ps.sl_warning_sent = False
        ps.persist(cid, pair)
        sl = ai.get("stop_loss",   round(price_val * (1 - cfg["sl_pct"] / 100), 2))
        tp = ai.get("take_profit", round(price_val * (1 + cfg["tp_pct"] / 100), 2))
        # Save to signals table for backtesting
        db_save_signal(
            pair, dr, price_val, float(sl), float(tp),
            a.get("score", 0), ai.get("sentiment", "neutral"), source="user",
        )
        await safe_edit(
            q,
            f"✅ *{cfg['emoji']} Trade opened!*\n\n"
            f"{'🟢' if dr == 'BUY' else '🔴'} Direction: *{dr}* {de}\n"
            f"Entry: *{fmt_price(price_val, pair)}*\n"
            f"🛑 SL: *{sl}*  🎯 TP: *{tp}*",
        )
        return

    if q.data.startswith("wait_"):
        try:
            opt = float(q.data[5:])
        except ValueError:
            await safe_edit(q, "❌ Error.", markup=kb_main_for(cid, plan, pair))
            return
        ps.waiting_entry_price = opt
        ps.persist(cid, pair)
        await safe_edit(q, f"⏳ Waiting for *{fmt_price(opt, pair)}*",
                        markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "cancel":
        u.pending_analysis = None
        await safe_edit(q, "↩️ Cancelled", markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "stop":
        ps.running = False
        ps.persist(cid, pair)
        await safe_edit(q, "⏹ Stopped", markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "reset":
        ps.reset(cid, pair)
        u.pending_analysis = None
        await safe_edit(q, "🔄 *Reset*", markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "status":
        lines = []
        for pid, pst in u.pairs.items():
            pr = _prices.get(pid)
            if pst.has_trade and pr:
                ch = (pr - pst.entry_price) / pst.entry_price * 100
                lines.append(
                    f"{PAIRS[pid]['emoji']} *{PAIRS[pid]['name']}* "
                    f"{'🟢' if ch >= 0 else '🔴'} *{ch:+.2f}%*"
                )
        msg = "\n\n".join(lines) if lines else "ℹ️ No active trades"
        await safe_edit(q, f"📊 *Status*\n\n{msg}", markup=kb_main_for(cid, plan, u.selected_pair))
        return

    # ── Signal accuracy stats ────────────────────────────────────
    if q.data.startswith("stats_"):
        await _handle_stats_callback(q, cid, q.data)
        return

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid     = update.effective_chat.id
    payment = update.message.successful_payment
    stars   = payment.total_amount
    payload = payment.invoice_payload
    charge  = payment.telegram_payment_charge_id
    parts   = payload.split("_")
    pk      = parts[0]
    months  = int(parts[1]) if len(parts) > 1 else 1
    new_exp = db_apply_payment(cid, stars, pk, months, charge)
    uname   = update.effective_user.username or str(cid)
    log.info("💰 PAYMENT: @%s | %s x%d mo | %d⭐ | until %s",
             uname, plan_label(pk), months, stars, new_exp.strftime("%d.%m.%Y"))
    await update.message.reply_text(
        f"✅ *Payment received!*\n\n"
        f"{PLAN_EMOJI.get(pk, '⭐')} *{plan_label(pk)}*\n"
        f"Active until: *{new_exp.strftime('%d.%m.%Y')}*\n"
        f"⭐ {stars} Stars\n\nTap /start",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💰 *New payment!*\n@{uname} | {plan_label(pk)} x{months} mo | {stars}⭐\n"
            f"Until: {new_exp.strftime('%d.%m.%Y')}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Admin notification failed: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  Background monitor
# ═══════════════════════════════════════════════════════════════════

async def monitor(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_channel_post_hour, _last_article_hour, _article_index

    if _monitor_lock.locked():
        log.warning("monitor: already running, skipping tick")
        return

    async with _monitor_lock:
        # 1) Update prices
        for pair in PAIRS:
            p = get_price(pair)
            if p:
                _prev_prices[pair] = _prices[pair]
                _prices[pair]      = p

        now_utc = datetime.now(UTC)
        h = now_utc.hour

        # 2) Channel market-analysis posts — Groq only in CHANNEL_HOURS_UTC (~few times/day),
        #    using GROQ_MODEL_NEWS (high daily quota), not every monitor tick.
        if h in CHANNEL_HOURS_UTC and h != _last_channel_post_hour:
            _last_channel_post_hour = h
            for pair in PAIRS:
                price = _prices.get(pair)
                if not price:
                    continue
                try:
                    a    = await asyncio.to_thread(
                        full_analysis, price, _prev_prices.get(pair), pair, GROQ_MODEL_NEWS,
                    )
                    text = groq_channel_post(a, post_type_for_hour(h))
                    cfg  = PAIRS[pair]
                    sent = await safe_send_photo(context.bot, CHANNEL_ID, cfg["image"], text)
                    if not sent:
                        await safe_send(context.bot, CHANNEL_ID, text)
                    db_save_post(pair, post_type_for_hour(h),
                                 a["score"], a["ai"].get("sentiment", "?"), price, 0)
                    # Save to signals table for backtesting
                    ai  = a["ai"]
                    dr, _ = _direction(ai, a["trend"], a.get("tech"))
                    sl  = ai.get("stop_loss",   round(price * (1 - cfg["sl_pct"] / 100), 2))
                    tp  = ai.get("take_profit", round(price * (1 + cfg["tp_pct"] / 100), 2))
                    db_save_signal(pair, dr, price, float(sl), float(tp),
                                   a["score"], ai.get("sentiment", "neutral"), source="ai")
                    log.info("Channel: analysis %s published (score=%s)", pair, a["score"])
                except Exception as e:
                    log.error("Channel post error (%s): %s", pair, e)

        # 3) Educational / news articles — Groq only in ARTICLE_HOURS_UTC, GROQ_MODEL_NEWS via groq_article().
        if h in ARTICLE_HOURS_UTC and h != _last_article_hour:
            _last_article_hour = h
            topic_type = "edu" if _article_index % 2 == 0 else "news"
            if topic_type == "edu":
                topic = EDU_TOPICS[(_article_index // 2) % len(EDU_TOPICS)]
            else:
                topic = NEWS_TOPICS[(_article_index // 2) % len(NEWS_TOPICS)]
            _article_index += 1
            try:
                body = await asyncio.to_thread(groq_article, topic_type, topic)
                text = format_article_post(topic_type, body)
                await send_article_with_image(context.bot, CHANNEL_ID, topic_type, topic, text)
                log.info("Channel: article [%s] published: %s", topic_type, topic[:50])
            except Exception as e:
                log.error("Article post error: %s", e)

        # 4) Per-user trade monitoring + auto-signals (GROQ_MODEL_SIGNALS via full_analysis default)
        for cid, u in list(USERS.items()):
            acc = db_access(cid)
            if not acc["allowed"]:
                continue
            plan = acc["plan"]

            for pair, ps in u.pairs.items():
                price = _prices.get(pair)
                if not price:
                    continue
                cfg = PAIRS[pair]

                if ps.has_trade:
                    ch = (price - ps.entry_price) / ps.entry_price * 100
                    if ch <= -cfg["sl_pct"]:
                        await safe_send(context.bot, cid,
                            f"❌ *STOP LOSS {cfg['emoji']} {cfg['name']}*\n"
                            f"Price: *{fmt_price(price, pair)}* | PnL: *{ch:+.2f}%*")
                        ps.reset(cid, pair)
                    elif ch >= cfg["tp_pct"]:
                        await safe_send(context.bot, cid,
                            f"✅ *TAKE PROFIT {cfg['emoji']} {cfg['name']}*\n"
                            f"Price: *{fmt_price(price, pair)}* | PnL: *{ch:+.2f}%*")
                        ps.reset(cid, pair)
                    elif ch <= -cfg["sl_pct"] * 0.75 and not ps.sl_warning_sent:
                        await safe_send(context.bot, cid,
                            f"⚠️ Approaching SL on {cfg['name']}\nPnL: *{ch:+.2f}%*")
                        ps.sl_warning_sent = True
                        ps.persist(cid, pair)

                if ps.is_waiting and price <= ps.waiting_entry_price:
                    ps.entry_price         = ps.waiting_entry_price
                    ps.running             = True
                    ps.waiting_entry_price = None
                    ps.persist(cid, pair)
                    await safe_send(context.bot, cid,
                        f"🎯 *Level reached! {cfg['emoji']} {cfg['name']}*\n"
                        f"Entry: *{fmt_price(ps.entry_price, pair)}*")

                if (plan in ("pro", "diamond", "admin")
                        and not ps.has_trade and not ps.is_waiting):
                    cooldown   = AUTO_COOLDOWN // 2 if plan in ("diamond", "admin") else AUTO_COOLDOWN
                    score_min  = 65 if plan in ("diamond", "admin") else 75
                    if time.time() - ps.last_signal_time > cooldown:
                        prev = _prev_prices.get(pair)
                        if prev:
                            a = await asyncio.to_thread(
                                full_analysis, price, prev, pair,
                            )
                            if a["score"] >= score_min and a["score"] > ps.last_signal_score:
                                priority_tag = " 💠 *Priority*" if plan == "diamond" else ""
                                text = (build_analysis_text(a)
                                        + f"\n\n📡 *Auto-signal!*{priority_tag} Score: *{a['score']}/100*")
                                await safe_send(context.bot, cid, text,
                                                reply_markup=kb_main_for(cid, plan, pair))
                                ps.last_signal_time  = time.time()
                                ps.last_signal_score = a["score"]
                                ps.persist(cid, pair)


# ═══════════════════════════════════════════════════════════════════
#  Signal tracking helpers
# ═══════════════════════════════════════════════════════════════════

def db_save_signal(pair: str, direction: str, entry: float,
                   sl: float, tp: float, score: int,
                   sentiment: str, source: str = "ai",
                   message_id: int = 0) -> int:
    """Save a new signal and return its id."""
    with db_connect() as c:
        cur = c.execute(
            "INSERT INTO signals(pair,direction,entry_price,sl_price,tp_price,"
            "score,sentiment,source,message_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (pair, direction, entry, sl, tp, score, sentiment, source, message_id),
        )
        return cur.lastrowid


def db_get_open_signals(days: int = 30) -> list:
    """Get unresolved signals from the last N days."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    with db_connect() as c:
        return c.execute(
            "SELECT * FROM signals WHERE outcome IS NULL AND posted_at >= ?",
            (cutoff,),
        ).fetchall()


def db_resolve_signal(sig_id: int, outcome: str, pnl_pct: float) -> None:
    with db_connect() as c:
        c.execute(
            "UPDATE signals SET outcome=?, pnl_pct=?, resolved_at=datetime('now') WHERE id=?",
            (outcome, pnl_pct, sig_id),
        )


def db_backtest_stats(pair: str | None = None, days: int = 30) -> dict:
    """Return win/loss/pending stats for resolved signals."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    with db_connect() as c:
        q_base = "FROM signals WHERE posted_at >= ?"
        params: list = [cutoff]
        if pair:
            q_base += " AND pair = ?"
            params.append(pair)
        total    = c.execute(f"SELECT COUNT(*) {q_base}", params).fetchone()[0]
        wins     = c.execute(f"SELECT COUNT(*) {q_base} AND outcome='TP'",  params).fetchone()[0]
        losses   = c.execute(f"SELECT COUNT(*) {q_base} AND outcome='SL'",  params).fetchone()[0]
        pending  = c.execute(f"SELECT COUNT(*) {q_base} AND outcome IS NULL", params).fetchone()[0]
        avg_pnl_row = c.execute(
            f"SELECT AVG(pnl_pct) {q_base} AND outcome IS NOT NULL", params
        ).fetchone()[0]
    resolved  = wins + losses
    win_rate  = round(wins / resolved * 100, 1) if resolved else 0.0
    avg_pnl   = round(avg_pnl_row or 0.0, 2)
    return dict(total=total, wins=wins, losses=losses, pending=pending,
                resolved=resolved, win_rate=win_rate, avg_pnl=avg_pnl)


# ═══════════════════════════════════════════════════════════════════
#  Backtesting engine  (runs in executor — never on event loop)
# ═══════════════════════════════════════════════════════════════════

def _resolve_open_signals() -> int:
    """
    For every unresolved signal check historical data to see if
    TP or SL was hit. Returns count of newly resolved signals.
    """
    try:
        import yfinance as yf
    except ImportError:
        return 0

    open_sigs = db_get_open_signals(days=30)
    resolved  = 0

    for sig in open_sigs:
        pair      = sig["pair"]
        direction = sig["direction"]
        entry     = sig["entry_price"]
        sl        = sig["sl_price"]
        tp        = sig["tp_price"]
        posted_at = sig["posted_at"]

        try:
            # Download 1-minute bars from signal time to now
            ticker = PAIRS[pair]["yahoo"]
            start  = datetime.strptime(posted_at[:19], "%Y-%m-%d %H:%M:%S")
            df     = yf.download(
                ticker, start=start, interval="5m",
                progress=False, auto_adjust=True,
            )
            if df.empty or len(df) < 2:
                continue
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)

            highs  = df["High"].values.flatten()
            lows   = df["Low"].values.flatten()
            closes = df["Close"].values.flatten()

            outcome = None
            pnl_pct = 0.0

            for i in range(len(df)):
                h, l = float(highs[i]), float(lows[i])
                if direction == "BUY":
                    if l <= sl:
                        outcome = "SL"
                        pnl_pct = round((sl - entry) / entry * 100, 2)
                        break
                    if h >= tp:
                        outcome = "TP"
                        pnl_pct = round((tp - entry) / entry * 100, 2)
                        break
                else:  # SELL
                    if h >= sl:
                        outcome = "SL"
                        pnl_pct = round((entry - sl) / entry * 100, 2)
                        break
                    if l <= tp:
                        outcome = "TP"
                        pnl_pct = round((entry - tp) / entry * 100, 2)
                        break

            if outcome is None:
                # Signal still open — check if price moved past last close
                last = float(closes[-1])
                if direction == "BUY":
                    pnl_pct = round((last - entry) / entry * 100, 2)
                else:
                    pnl_pct = round((entry - last) / entry * 100, 2)
                # Only mark as expired if older than 24h
                age_h = (datetime.now(UTC) - start.replace(tzinfo=UTC)).total_seconds() / 3600
                if age_h >= 24:
                    outcome = "EXPIRED"

            if outcome:
                db_resolve_signal(sig["id"], outcome, pnl_pct)
                resolved += 1

        except Exception as e:
            log.debug("Backtest resolve error (signal %s): %s", sig["id"], e)

    return resolved


def _backtest_stats_text(pair: str | None = None, days: int = 30) -> str:
    """Format backtest stats as Markdown string."""
    stats = db_backtest_stats(pair, days)
    pair_label = PAIRS[pair]["name"] if pair else "All pairs"
    emoji      = PAIRS[pair]["emoji"] if pair else "📊"
    wr         = stats["win_rate"]

    if stats["resolved"] == 0:
        return (
            f"{emoji} *{pair_label} — Signal Accuracy*\n"
            f"{'─' * 28}\n\n"
            f"📭 Not enough resolved signals yet.\n"
            f"Check back after a few days of operation."
        )

    bar_w = round(wr / 10)
    bar   = "🟢" * bar_w + "⬜" * (10 - bar_w)

    verdict = (
        "🏆 Excellent"  if wr >= 70 else
        "✅ Good"        if wr >= 55 else
        "⚠️ Average"    if wr >= 45 else
        "🔴 Below average"
    )

    return (
        f"{emoji} *{pair_label} — {days}d Signal Accuracy*\n"
        f"{'─' * 28}\n\n"
        f"📈 Win rate: *{wr}%* {verdict}\n"
        f"{bar}\n\n"
        f"✅ TP hit: *{stats['wins']}*\n"
        f"❌ SL hit: *{stats['losses']}*\n"
        f"⏳ Pending: *{stats['pending']}*\n"
        f"📊 Avg P&L per signal: *{stats['avg_pnl']:+.2f}%*\n\n"
        f"_Based on {stats['resolved']} resolved signals "
        f"over the last {days} days_"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show signal accuracy stats. Available to all users."""
    cid = update.effective_chat.id
    acc = db_access(cid)
    if not acc["allowed"] and cid != ADMIN_ID:
        await update.message.reply_text(
            "⛔ Subscribe to view signal accuracy stats.",
            reply_markup=kb_sub(),
        )
        return

    args = context.args or []
    pair = args[0].upper() if args and args[0].upper() in PAIRS else None
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30

    # Resolve any open signals first (in executor to avoid blocking)
    loop = asyncio.get_event_loop()
    resolved_count = await loop.run_in_executor(None, _resolve_open_signals)

    text = _backtest_stats_text(pair, days)
    if resolved_count:
        text += f"\n\n_🔄 {resolved_count} signal(s) just resolved_"

    # Add pair selector buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 All pairs",  callback_data="stats_ALL_30"),
         InlineKeyboardButton("🥇 XAU/USD",    callback_data="stats_XAUUSD_30")],
        [InlineKeyboardButton("₿ BTC/USD",     callback_data="stats_BTCUSD_30"),
         InlineKeyboardButton("Ξ ETH/USD",     callback_data="stats_ETHUSD_30")],
        [InlineKeyboardButton("📅 7 days",     callback_data=f"stats_{pair or 'ALL'}_7"),
         InlineKeyboardButton("📅 30 days",    callback_data=f"stats_{pair or 'ALL'}_30"),
         InlineKeyboardButton("📅 90 days",    callback_data=f"stats_{pair or 'ALL'}_90")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════
#  TradingView webhook  (aiohttp server alongside PTB)
# ═══════════════════════════════════════════════════════════════════

TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "change_this_secret_123")
TV_WEBHOOK_PORT   = int(os.getenv("TV_WEBHOOK_PORT", "8080"))

# Pine Script template printed at /forcepost --help
PINE_SCRIPT_TEMPLATE = """
// ── TradingView Pine Script webhook template ──
// Add this to your strategy's alertcondition

// In Alert settings → Webhook URL:
//   http://YOUR_SERVER_IP:{port}/tv
//
// Message body (JSON):
alertcondition(
    condition  = strategy.position_size > 0,
    title      = "Bot Signal",
    message    = '{{"secret":"{secret}","pair":"XAUUSD","direction":"BUY","entry":{{{{close}}}},"sl":{{{{strategy.position_avg_price * 0.98}}}},"tp":{{{{strategy.position_avg_price * 1.03}}}},"score":80,"source":"tradingview"}}'
)
""".format(port=TV_WEBHOOK_PORT, secret=TV_WEBHOOK_SECRET)


async def _tv_webhook_handler(request) -> "web.Response":
    """Handle incoming TradingView alerts."""
    from aiohttp import web
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    # Authenticate
    if data.get("secret") != TV_WEBHOOK_SECRET:
        log.warning("TV webhook: wrong secret from %s", request.remote)
        return web.Response(status=403, text="Forbidden")

    # Parse signal
    try:
        pair      = str(data.get("pair", "XAUUSD")).upper()
        direction = str(data.get("direction", "BUY")).upper()
        entry     = float(data["entry"])
        sl        = float(data["sl"])
        tp        = float(data["tp"])
        score     = int(data.get("score", 75))
        source    = str(data.get("source", "tradingview"))
        sentiment = "bullish" if direction == "BUY" else "bearish"

        if pair not in PAIRS:
            return web.Response(status=400, text=f"Unknown pair: {pair}")
        if direction not in ("BUY", "SELL"):
            return web.Response(status=400, text="direction must be BUY or SELL")
        if entry <= 0 or sl <= 0 or tp <= 0:
            return web.Response(status=400, text="entry/sl/tp must be > 0")

    except (KeyError, ValueError, TypeError) as e:
        return web.Response(status=400, text=f"Bad params: {e}")

    cfg     = PAIRS[pair]
    dir_e   = "📈" if direction == "BUY" else "📉"
    dir_col = "🟢" if direction == "BUY" else "🔴"
    rr_pct  = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

    text = (
        f"📡 *TradingView Signal* | {cfg['emoji']} {cfg['name']}\n"
        f"{'─' * 28}\n\n"
        f"💰 Entry: *{fmt_price(entry, pair)}*\n"
        f"{dir_col} Direction: *{direction}* {dir_e}\n"
        f"🛑 SL: *{fmt_price(sl, pair)}*\n"
        f"🎯 TP: *{fmt_price(tp, pair)}*\n"
        f"📐 R/R: *1:{rr_pct:.1f}*\n\n"
        f"📊 Score: `{score_bar(score)}`  *{score}/100*\n\n"
        f"⚡ _Source: Pine Script strategy_\n\n"
        f"▶️ Details → {BOT_USERNAME}"
    )

    # Get the bot application from global context
    app_ref = _get_app_ref()
    if app_ref is None:
        return web.Response(status=503, text="Bot not ready")

    try:
        # Post to channel with pair image
        msg = await app_ref.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=cfg["image"],
            caption=fix_markdown(text),
            parse_mode="Markdown",
        )
    except Exception:
        try:
            msg = await safe_send(app_ref.bot, CHANNEL_ID, text)
        except Exception as e:
            log.error("TV webhook channel post failed: %s", e)
            return web.Response(status=500, text="Channel post failed")

    # Save signal for backtesting
    sig_id = db_save_signal(
        pair, direction, entry, sl, tp, score, sentiment,
        source=source, message_id=getattr(msg, "message_id", 0),
    )

    # Notify Pro users
    notified = 0
    for cid, u in list(USERS.items()):
        acc = db_access(cid)
        if acc["plan"] in ("pro", "admin"):
            try:
                await safe_send(
                    app_ref.bot, cid,
                    f"⚡ *New TradingView Signal!*\n\n{text}",
                    reply_markup=kb_main_for(cid, acc["plan"], pair),
                )
                notified += 1
            except Exception:
                pass

    log.info("TV webhook: %s %s @ %s — signal #%d, notified %d Pro users",
             direction, pair, entry, sig_id, notified)
    return web.Response(text=f"ok signal_id={sig_id}")


async def _nowpayments_ipn_handler(request) -> "web.Response":
    """
    Handle NOWPayments IPN callbacks.
    Validates HMAC-SHA512 signature (x-nowpayments-sig) using NOWPAYMENTS_IPN_SECRET.
    """
    from aiohttp import web
    if not NOWPAYMENTS_IPN_SECRET:
        return web.Response(status=503, text="IPN not configured")

    sig = request.headers.get("x-nowpayments-sig", "")
    raw = await request.read()
    if not sig:
        return web.Response(status=403, text="Missing signature")

    # NOWPayments signature: HMAC-SHA512 over raw body using IPN secret
    mac = hmac.new(NOWPAYMENTS_IPN_SECRET.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(mac, sig):
        log.warning("NOWPayments IPN: bad signature from %s", request.remote)
        return web.Response(status=403, text="Bad signature")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    payment_id = str(data.get("payment_id") or "")
    status = str(data.get("payment_status") or data.get("status") or "")
    if not payment_id:
        return web.Response(status=400, text="Missing payment_id")

    row = db_crypto_payment_get(payment_id)
    if not row:
        # Payment might have been created elsewhere; ignore to avoid leaking info
        log.info("NOWPayments IPN: unknown payment_id=%s", payment_id)
        return web.Response(text="ok")

    # Update DB record
    db_crypto_payment_upsert(
        payment_id=payment_id,
        chat_id=int(row["chat_id"]),
        plan=str(row["plan"]),
        months=int(row["months"]),
        price_usd=float(row["price_usd"]),
        pay_currency=str(row["pay_currency"]),
        pay_amount=float(data.get("pay_amount")) if data.get("pay_amount") else row["pay_amount"],
        pay_address=str(data.get("pay_address") or row["pay_address"]),
        status=status or str(row["status"]),
    )

    charge_id = f"nowp:{payment_id}"
    if _nowp_is_paid(status) and not db_payment_exists(charge_id):
        try:
            new_exp = db_apply_payment(int(row["chat_id"]), 0, str(row["plan"]), int(row["months"]), charge_id)
            app_ref = _get_app_ref()
            if app_ref is not None:
                await safe_send(
                    app_ref.bot,
                    int(row["chat_id"]),
                    f"✅ *Crypto payment confirmed!*\\n\\n"
                    f"{PLAN_EMOJI.get(str(row['plan']), '💳')} *{plan_label(str(row['plan']))}*\\n"
                    f"Active until: *{new_exp.strftime('%d.%m.%Y')}*\\n\\nTap /start",
                )
        except Exception as e:
            log.error("NOWPayments IPN activation failed: %s", e)

    return web.Response(text="ok")


# Global reference to PTB Application for webhook handler
_APP_REF = None


def _get_app_ref():
    return _APP_REF


async def _start_webhook_server() -> None:
    """
    Start aiohttp webhook server (shared).
    - TradingView: /tv (requires TV_WEBHOOK_SECRET)
    - NOWPayments: /nowpayments (requires NOWPAYMENTS_IPN_SECRET)
    """
    tv_enabled = bool(TV_WEBHOOK_SECRET and TV_WEBHOOK_SECRET != "change_this_secret_123")
    nowp_enabled = bool(NOWPAYMENTS_IPN_SECRET)
    if not (tv_enabled or nowp_enabled):
        log.info("Webhook server disabled (no TV_WEBHOOK_SECRET / NOWPAYMENTS_IPN_SECRET)")
        return
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not installed — webhook server disabled.")
        return

    app_web = web.Application()
    if tv_enabled:
        app_web.router.add_post("/tv", _tv_webhook_handler)
    if nowp_enabled:
        app_web.router.add_post("/nowpayments", _nowpayments_ipn_handler)

    async def health(request):
        return web.Response(text="ok")
    app_web.router.add_get("/health", health)

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", TV_WEBHOOK_PORT)
    await site.start()
    routes = []
    if tv_enabled:
        routes.append("/tv")
    if nowp_enabled:
        routes.append("/nowpayments")
    log.info("✅ Webhook server listening on port %d (%s)", TV_WEBHOOK_PORT, ", ".join(routes))


async def cmd_tvinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: show TradingView webhook setup instructions."""
    if update.effective_chat.id != ADMIN_ID:
        return
    text = (
        f"📡 *TradingView Webhook Setup*\n"
        f"{'─' * 28}\n\n"
        f"*1. Webhook URL:*\n"
        f"`http://YOUR\\_SERVER\\_IP:{TV_WEBHOOK_PORT}/tv`\n\n"
        f"*2. Secret* (in .env):\n"
        f"`TV_WEBHOOK_SECRET={TV_WEBHOOK_SECRET}`\n\n"
        f"*3. Pine Script alert message (JSON):*\n"
        f"```\n"
        f'{{\n'
        f'  "secret": "{TV_WEBHOOK_SECRET}",\n'
        f'  "pair": "XAUUSD",\n'
        f'  "direction": "BUY",\n'
        f'  "entry": {{{{close}}}},\n'
        f'  "sl": {{{{close}}}} * 0.98,\n'
        f'  "tp": {{{{close}}}} * 1.03,\n'
        f'  "score": 80\n'
        f'}}\n'
        f"```\n\n"
        f"*4. Supported pairs:*\n"
        f"XAUUSD · BTCUSD · ETHUSD\n\n"
        f"*5. Test with curl:*\n"
        f"```\n"
        f"curl -X POST http://YOUR_SERVER_IP:{TV_WEBHOOK_PORT}/tv \\\\\n"
        f"  -H 'Content-Type: application/json' \\\\\n"
        f"  -d '{{\"secret\":\"{TV_WEBHOOK_SECRET}\",\"pair\":\"XAUUSD\","
        f"\"direction\":\"BUY\",\"entry\":3300,\"sl\":3234,\"tp\":3399,\"score\":82}}'\n"
        f"```"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  Backtest button handler (stats_PAIR_DAYS)
# ═══════════════════════════════════════════════════════════════════

async def _handle_stats_callback(q, cid: int, data: str) -> None:
    """Handle stats_PAIR_DAYS callback queries."""
    parts = data.split("_")          # ["stats", "XAUUSD", "30"]
    pair  = parts[1] if len(parts) > 1 else "ALL"
    days  = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
    pair_filter = pair if pair != "ALL" else None

    loop = asyncio.get_event_loop()
    resolved_count = await loop.run_in_executor(None, _resolve_open_signals)

    text = _backtest_stats_text(pair_filter, days)
    if resolved_count:
        text += f"\n\n_🔄 {resolved_count} signal(s) just resolved_"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 All pairs",  callback_data="stats_ALL_30"),
         InlineKeyboardButton("🥇 XAU/USD",    callback_data="stats_XAUUSD_30")],
        [InlineKeyboardButton("₿ BTC/USD",     callback_data="stats_BTCUSD_30"),
         InlineKeyboardButton("Ξ ETH/USD",     callback_data="stats_ETHUSD_30")],
        [InlineKeyboardButton("📅 7d",  callback_data=f"stats_{pair}_7"),
         InlineKeyboardButton("📅 30d", callback_data=f"stats_{pair}_30"),
         InlineKeyboardButton("📅 90d", callback_data=f"stats_{pair}_90")],
    ])
    await safe_edit(q, text, markup=kb)

async def cmd_forcearticle(update, context):
    if update.effective_chat.id != ADMIN_ID:
        return
    global _article_index
    args = context.args or []
    topic_type = args[0].lower() if args and args[0].lower() in ("news", "edu") else "edu"
    await update.message.reply_text(f"Generating {topic_type} article...")
    if topic_type == "news":
        topic = NEWS_TOPICS[(_article_index // 2) % len(NEWS_TOPICS)]
    else:
        topic = EDU_TOPICS[(_article_index // 2) % len(EDU_TOPICS)]
    _article_index += 1
    try:
        body = await asyncio.to_thread(groq_article, topic_type, topic)
        text = format_article_post(topic_type, body)
        await send_article_with_image(context.bot, CHANNEL_ID, topic_type, topic, text)
        await update.message.reply_text(f"Published! Type: {topic_type} Topic: {topic[:60]}")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:120]}")


def main() -> None:
    global _APP_REF
    db_init()
    log.info("DB initialised. Starting bot…")
    log.info(
        "Groq: channel/articles=%s | user signals=%s | monitor interval=%ss",
        GROQ_MODEL_NEWS,
        GROQ_MODEL_SIGNALS,
        MONITOR_INTERVAL_SEC,
    )
    if _openrouter_configured():
        log.info(
            "OpenRouter: %d API key(s) — round-robin + auto-failover on quota (429)",
            len(_OPENROUTER_KEYS),
        )
    else:
        log.info("OpenRouter: not configured (optional; used after Groq 429 with Gemini fallback)")

    app = ApplicationBuilder().token(TOKEN).build()
    _APP_REF = app  # store reference for TV webhook handler

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("refer",        cmd_refer))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("tvinfo",       cmd_tvinfo))
    app.add_handler(CommandHandler("deepanalysis", cmd_deepanalysis))
    app.add_handler(CommandHandler("chart",        cmd_chartanalysis))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CommandHandler("give",         cmd_give))
    app.add_handler(CommandHandler("forcepost",    cmd_forcepost))
    app.add_handler(CommandHandler("forcearticle", cmd_forcearticle))
    app.add_handler(CommandHandler("welcome",      cmd_welcome))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.job_queue.run_repeating(monitor, interval=MONITOR_INTERVAL_SEC, first=15)

    # Start TradingView webhook server
    async def post_init(application):
        await _start_webhook_server()

    app.post_init = post_init

    log.info("✅ Bot running… Stop with Ctrl+C")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
