
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
Dependencies: pip install python-telegram-bot[job-queue] requests groq yfinance pandas pandas-ta python-dotenv
"""

import asyncio
import concurrent.futures
import csv
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
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
GEMINI_KEY   = (
    os.getenv("GEMINI_KEY")
    or os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY", "")
)
ADMIN_ID     = int(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID   = os.getenv("CHANNEL_ID",  "@your_channel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@your_bot")

GROQ_MODEL   = "llama-3.1-8b-instant"
GROQ_TIMEOUT = 20

# Deep analysis models
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-1.5-flash")
GEMINI_DEEP_MODEL = os.getenv("GEMINI_DEEP_MODEL", "gemini-1.5-pro")

PAID_PLANS = ("basic", "pro", "diamond")
TRADE_CONTROL_PLANS = (*PAID_PLANS, "admin")
AUTO_SIGNAL_PLANS = ("pro", "diamond", "admin")

TRIAL_DAYS        = 7
PRICE_BASIC       = 550    # ~$5 net after Telegram 30% fee
PRICE_PRO         = 1100   # ~$9.99 net
PRICE_BASIC_3     = 1375   # 3-month ~17% discount
PRICE_PRO_3       = 2750
PRICE_DIAMOND     = 2150   # ~$19.99/mo
PRICE_DIAMOND_3   = 5375   # 3mo ~17% off (~$49.99)
PLAN_PRICES_STARS = {
    ("basic", 1): PRICE_BASIC,
    ("basic", 3): PRICE_BASIC_3,
    ("pro", 1): PRICE_PRO,
    ("pro", 3): PRICE_PRO_3,
    ("diamond", 1): PRICE_DIAMOND,
    ("diamond", 3): PRICE_DIAMOND_3,
}
PLAN_PRICES_USD = {
    ("basic", 1): "5",
    ("basic", 3): "12.50",
    ("pro", 1): "9.99",
    ("pro", 3): "25",
    ("diamond", 1): "19.99",
    ("diamond", 3): "49.99",
}
PLAN_DESCRIPTIONS = {
    "basic": "XAU/XAG analysis",
    "pro": "All pairs + auto-signals",
    "diamond": "All pairs + priority analysis + auto-signals",
}
CRYPTO_WALLETS = {
    "USDT TRC20": os.getenv("CRYPTO_USDT_TRC20_ADDRESS", "").strip(),
    "USDT ERC20": os.getenv("CRYPTO_USDT_ERC20_ADDRESS", "").strip(),
    "BTC": os.getenv("CRYPTO_BTC_ADDRESS", "").strip(),
    "ETH": os.getenv("CRYPTO_ETH_ADDRESS", "").strip(),
    "TON": os.getenv("CRYPTO_TON_ADDRESS", "").strip(),
}
DB_PATH           = "users.db"
CHANNEL_HOURS_UTC = [6, 12, 18]   # market analysis posts (UTC)
ARTICLE_HOURS_UTC = [8, 14, 20]   # article posts — separate from analysis
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
            CREATE TABLE IF NOT EXISTS pending_crypto_payments (
                code         TEXT PRIMARY KEY,
                chat_id      INTEGER NOT NULL,
                plan         TEXT    NOT NULL,
                months       INTEGER NOT NULL,
                amount_usd   TEXT    NOT NULL,
                status       TEXT    DEFAULT 'pending',
                created_at   TEXT    DEFAULT (datetime('now')),
                confirmed_at TEXT,
                tx_hash      TEXT
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
        """)


def db_upsert_user(cid: int, username: str = "", fname: str = "") -> None:
    with db_connect() as c:
        row = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (cid,)).fetchone()
        if row is None:
            trial_ends = (datetime.utcnow() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
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

    today = datetime.utcnow().date()
    plan  = row["plan"]

    if cid == ADMIN_ID:
        return {"allowed": True, "plan": "admin", "days_left": 9999, "reason": ""}

    if plan in PAID_PLANS and row["sub_expires"]:
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


def db_apply_payment(cid: int, stars: int, plan_key: str, months: int, charge_id: str) -> date:
    if plan_key not in PAID_PLANS or months not in (1, 3):
        raise ValueError(f"Invalid paid plan payload: {plan_key}_{months}")
    today = datetime.utcnow().date()
    with db_connect() as c:
        row = c.execute("SELECT sub_expires FROM users WHERE chat_id=?", (cid,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO users(chat_id,plan,last_active) VALUES(?,'expired',datetime('now'))",
                (cid,),
            )
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


def db_apply_trial(cid: int, days: int, charge_id: str = "manual_trial") -> date:
    today = datetime.utcnow().date()
    trial_ends = today + timedelta(days=days)
    with db_connect() as c:
        if c.execute("SELECT 1 FROM users WHERE chat_id=?", (cid,)).fetchone() is None:
            c.execute(
                "INSERT INTO users(chat_id,plan,trial_ends,last_active) VALUES(?,'trial',?,datetime('now'))",
                (cid, trial_ends.strftime("%Y-%m-%d")),
            )
        else:
            c.execute(
                "UPDATE users SET plan='trial',trial_ends=?,last_active=datetime('now') WHERE chat_id=?",
                (trial_ends.strftime("%Y-%m-%d"), cid),
            )
        c.execute(
            "INSERT INTO payments(chat_id,stars,plan,months,telegram_charge_id) VALUES(?,?,?,?,?)",
            (cid, 0, "trial", max(days // 30, 1), charge_id),
        )
    return trial_ends


def _parse_plan_payload(payload: str) -> tuple[str, int, int] | None:
    try:
        plan_key, months_raw = payload.split("_", 1)
        months = int(months_raw)
    except (ValueError, AttributeError):
        return None
    stars = PLAN_PRICES_STARS.get((plan_key, months))
    if stars is None:
        return None
    return plan_key, months, stars


def _crypto_wallet_lines() -> list[str]:
    return [f"*{name}:* `{address}`" for name, address in CRYPTO_WALLETS.items() if address]


def db_create_crypto_payment(cid: int, plan_key: str, months: int) -> dict:
    if (plan_key, months) not in PLAN_PRICES_USD:
        raise ValueError(f"Invalid crypto plan: {plan_key}_{months}")
    code = secrets.token_hex(4).upper()
    amount = PLAN_PRICES_USD[(plan_key, months)]
    with db_connect() as c:
        c.execute(
            "INSERT INTO pending_crypto_payments(code,chat_id,plan,months,amount_usd) VALUES(?,?,?,?,?)",
            (code, cid, plan_key, months, amount),
        )
    return {"code": code, "plan": plan_key, "months": months, "amount_usd": amount}


def db_confirm_crypto_payment(code: str, tx_hash: str = "") -> dict | None:
    code = code.upper()
    with db_connect() as c:
        row = c.execute(
            "SELECT * FROM pending_crypto_payments WHERE code=? AND status='pending'",
            (code,),
        ).fetchone()
    if row is None:
        return None
    new_exp = db_apply_payment(
        int(row["chat_id"]), 0, row["plan"], int(row["months"]),
        f"crypto:{code}:{tx_hash or 'manual'}",
    )
    with db_connect() as c:
        c.execute(
            "UPDATE pending_crypto_payments SET status='confirmed',confirmed_at=datetime('now'),tx_hash=? WHERE code=?",
            (tx_hash, code),
        )
    return {"chat_id": int(row["chat_id"]), "plan": row["plan"], "months": int(row["months"]), "expires": new_exp}


def db_stats() -> dict:
    with db_connect() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        trial = c.execute("SELECT COUNT(*) FROM users WHERE plan='trial'").fetchone()[0]
        basic   = c.execute("SELECT COUNT(*) FROM users WHERE plan='basic'").fetchone()[0]
        pro     = c.execute("SELECT COUNT(*) FROM users WHERE plan='pro'").fetchone()[0]
        diamond = c.execute("SELECT COUNT(*) FROM users WHERE plan='diamond'").fetchone()[0]
        exp   = c.execute("SELECT COUNT(*) FROM users WHERE plan='expired'").fetchone()[0]
        stars = c.execute("SELECT SUM(stars) FROM payments").fetchone()[0] or 0
        posts = c.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0]
        pending_crypto = c.execute(
            "SELECT COUNT(*) FROM pending_crypto_payments WHERE status='pending'"
        ).fetchone()[0]
    return dict(total=total, trial=trial, basic=basic, pro=pro, diamond=diamond, expired=exp,
                total_stars=stars, posts=posts, pending_crypto=pending_crypto)


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

        today = datetime.utcnow().date()
        if u["plan"] in PAID_PLANS and u["sub_expires"]:
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
#  Groq analysis  (ONE call per full_analysis — never loops)
# ═══════════════════════════════════════════════════════════════════

def _groq_client():
    from groq import Groq
    return Groq(api_key=GROQ_KEY)


def _make_sl_tp(price: float, direction: str, sl_pct: float, tp_pct: float) -> tuple[float, float]:
    """Calculate SL and TP correctly based on trade direction."""
    if direction == "SELL":
        sl = round(price * (1 + sl_pct / 100), 2)   # SL above entry for SELL
        tp = round(price * (1 - tp_pct / 100), 2)   # TP below entry for SELL
    else:  # BUY
        sl = round(price * (1 - sl_pct / 100), 2)   # SL below entry for BUY
        tp = round(price * (1 + tp_pct / 100), 2)   # TP above entry for BUY
    return sl, tp


def _normalize_confidence(raw_conf) -> int:
    """Normalize confidence to 0-100 integer. Handles 0.85 → 85 and 85 → 85."""
    try:
        v = float(raw_conf)
        if v <= 1.0:          # Groq returned 0.0–1.0 fraction
            v = v * 100
        return max(0, min(100, int(round(v))))
    except (TypeError, ValueError):
        return 35


def groq_analysis(news_text: str, price: float, tech: dict,
                  trend: str, vol: str, pair: str) -> dict:
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
        raw = _groq_client().chat.completions.create(
            model=GROQ_MODEL, timeout=GROQ_TIMEOUT,
            messages=[{"role": "user", "content": (
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
            )}],
            temperature=0.3, max_tokens=280,
        ).choices[0].message.content
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
        log.warning("Groq analysis: %s", e)
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

    # Trend — bearish now subtracts points
    if trend == "up":     s += _SW["trend_up"]
    elif trend == "flat": s += _SW["trend_flat"]
    else:                 s += _SW["trend_down"]   # down = -10

    # Volatility
    if vol == "normal": s += _SW["vol_normal"]
    elif vol == "high": s += _SW["vol_high"]
    # chaos adds nothing

    # Technicals — only add when direction aligns with sentiment
    sent = (ai.get("sentiment") or "neutral").lower()
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


def full_analysis(price: float, prev: float | None, pair: str) -> dict:
    """
    Run all data fetching in parallel using threads.
    Total time = max(slowest request) instead of sum of all requests.
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

    # Groq call after parallel fetch (needs tech + news)
    ai    = groq_analysis(news, price, tech, trend, vol, pair)
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
    verdict = (
        "✅ Good entry opportunity" if score >= 75 else
        "⚠️ Neutral conditions"     if score >= 50 else
        "🔴 Stay on the sidelines"
    )
    lines = [
        f"{header} | {cfg['emoji']} {cfg['name']}",
        "",
        f"💰 Price: *{fmt_price(price, pair)}*",
        f"{'🟢' if dr == 'BUY' else '🔴'} *{dr}* {de}",
        f"{si} Sentiment: *{(ai.get('sentiment') or '?').upper()}*  "
        f"Confidence: *{ai.get('confidence', '?')}%*",
        f"📐 Entry: *{ai.get('optimal_entry', price)}* | "
        f"SL: *{ai.get('stop_loss', '?')}* | TP: *{ai.get('take_profit', '?')}*",
        "",
        f"📊 Score: `{score_bar(score)}`  *{score}/100*",
        "",
        verdict,
        "",
        f"▶️ Details → {BOT_USERNAME}",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  News RSS for articles
# ═══════════════════════════════════════════════════════════════════

_RSS_FEEDS = [
    ("Reuters",       "https://feeds.reuters.com/reuters/businessNews"),
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CryptoNews",    "https://cryptonews.com/news/feed/"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
]
_NEWS_KW = ["gold", "xau", "bitcoin", "btc", "crypto", "ethereum",
            "eth", "fed", "inflation", "market", "trading", "usd"]


def get_news_rss() -> list[dict]:
    cutoff  = datetime.utcnow() - timedelta(hours=48)
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
                    pub_dt = parsedate_to_datetime(pub_str).replace(tzinfo=None)
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
        from_dt = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
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
]

NEWS_TOPICS = [
    "gold price Fed inflation USD",
    "bitcoin crypto market ETF",
    "ethereum DeFi crypto news",
    "central bank interest rates gold",
    "crypto regulation market impact",
]


def groq_article(topic_type: str, topic: str) -> str:
    """Single Groq call to generate an article. Raises on error."""
    if topic_type == "news":
        news = get_news_for_article(topic)
        news_block = f"Recent news (last 48h):\n{news[:800]}\n\n" if news else ""
        prompt = (
            f"You are a financial journalist. Topic: {topic}\n"
            f"{news_block}"
            "Write a concise news article in English for a Telegram trading channel.\n"
            "Structure:\n"
            "1. *Bold headline* (1 sentence)\n"
            "2. Main news — what happened (2-3 sentences)\n"
            "3. Market context — numbers and causes (2-3 sentences)\n"
            "4. Market impact — how prices may react (1-2 sentences)\n"
            "5. 📌 Trader tip — one concrete action (1 sentence)\n\n"
            "Length: 120-180 words. Use ONLY *bold* for the headline."
        )
    else:
        prompt = (
            f"You are an experienced trader and educator. Topic: {topic}\n"
            "Write a concise educational post in English for a Telegram trading channel.\n"
            "Structure:\n"
            "1. *Bold headline* (1 sentence)\n"
            "2. Definition — what it is (1-2 sentences)\n"
            "3. How it works — with a concrete example (2-3 sentences)\n"
            "4. Why traders need it (1-2 sentences)\n"
            "5. ⚠️ Common mistake to avoid (1 sentence)\n"
            "6. 📌 Practical tip (1 sentence)\n\n"
            "Length: 120-160 words. Use ONLY *bold* for the headline."
        )
    result = _groq_client().chat.completions.create(
        model=GROQ_MODEL, timeout=GROQ_TIMEOUT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6, max_tokens=500,
    ).choices[0].message.content.strip()
    return result


def format_article_post(topic_type: str, body: str) -> str:
    div = "─" * 28
    if topic_type == "edu":
        header = f"📚 *Educational Post*\n{div}"
        footer = f"\n{div}\n🤖 Trade with AI → {BOT_USERNAME}"
    else:
        header = f"📰 *Market News*\n{div}"
        footer = f"\n{div}\n📊 Signals & analysis → {BOT_USERNAME}"
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


# ═══════════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════════

PLAN_EMOJI = {
    "trial": "🔬", "basic": "⭐", "pro": "💎", "diamond": "💠",
    "admin": "👑", "expired": "❌",
}


def plan_label(p: str) -> str:
    return {"trial": "Trial", "basic": "Basic", "pro": "Pro", "diamond": "Diamond",
            "admin": "Admin", "expired": "Expired"}.get(p, p)


def kb_main(plan: str = "trial", pair: str = DEFAULT_PAIR) -> InlineKeyboardMarkup:
    cfg = PAIRS[pair]
    rows = [
        [InlineKeyboardButton(f"🔀 Pair: {cfg['emoji']} {cfg['name']}", callback_data="choose_pair")],
        [InlineKeyboardButton("▶️ Analyse & Enter", callback_data="start")],
    ]
    if plan in TRADE_CONTROL_PLANS:
        rows.append([
            InlineKeyboardButton("⏹ Stop",  callback_data="stop"),
            InlineKeyboardButton("🔄 Reset", callback_data="reset"),
        ])
        rows.append([InlineKeyboardButton("📊 Trade Status", callback_data="status")])
    rows.append([
        InlineKeyboardButton("💳 Subscription", callback_data="sub_menu"),
        InlineKeyboardButton("🤝 Refer & Earn",  callback_data="refer"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_pairs(current_pair: str, plan: str) -> InlineKeyboardMarkup:
    rows = []
    for pid, cfg in PAIRS.items():
        accessible = plan in cfg["plans"]
        mark  = "✅" if pid == current_pair else ("🔒" if not accessible else "")
        label = f"{mark} {cfg['emoji']} {cfg['name']}" + (" (Pro/Diamond)" if not accessible else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"pair_{pid}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_sub() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Basic — {PRICE_BASIC}⭐/mo (~$5)",          callback_data="buy_basic_1")],
        [InlineKeyboardButton(f"⭐ Basic — {PRICE_BASIC_3}⭐/3mo (~$12.5) 🔥", callback_data="buy_basic_3")],
        [InlineKeyboardButton(f"💎 Pro   — {PRICE_PRO}⭐/mo (~$9.99)",          callback_data="buy_pro_1")],
        [InlineKeyboardButton(f"💎 Pro   — {PRICE_PRO_3}⭐/3mo (~$25) 🔥",     callback_data="buy_pro_3")],
        [InlineKeyboardButton(f"💠 Diamond — {PRICE_DIAMOND}⭐/mo (~$19.99)",   callback_data="buy_diamond_1")],
        [InlineKeyboardButton(f"💠 Diamond — {PRICE_DIAMOND_3}⭐/3mo (~$49.99) 🔥", callback_data="buy_diamond_3")],
        [InlineKeyboardButton("₿ Pay directly with crypto", callback_data="crypto_menu")],
        [InlineKeyboardButton("↩️ Back", callback_data="back_main")],
    ])


def kb_crypto_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Basic — $5 / 1 mo", callback_data="crypto_basic_1")],
        [InlineKeyboardButton("⭐ Basic — $12.50 / 3 mo", callback_data="crypto_basic_3")],
        [InlineKeyboardButton("💎 Pro — $9.99 / 1 mo", callback_data="crypto_pro_1")],
        [InlineKeyboardButton("💎 Pro — $25 / 3 mo", callback_data="crypto_pro_3")],
        [InlineKeyboardButton("💠 Diamond — $19.99 / 1 mo", callback_data="crypto_diamond_1")],
        [InlineKeyboardButton("💠 Diamond — $49.99 / 3 mo", callback_data="crypto_diamond_3")],
        [InlineKeyboardButton("↩️ Back", callback_data="sub_menu")],
    ])


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
                  "🥇 XAU/USD — ✅", "₿ BTC — ✅", "Ξ ETH — ✅", ""]
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
                  "✅ Auto-signals", "✅ Premium deep/chart analysis", ""]
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
        "  • Premium deep/chart analysis",
        "  • Highest priority alerts",
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
        reply_markup=kb_main(plan, DEFAULT_PAIR),
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
        f"⭐ Basic: {s['basic']}\n💎 Pro: {s['pro']}\n"
        f"💠 Diamond: {s['diamond']}\n❌ Expired: {s['expired']}\n\n"
        f"📨 Posts: {s['posts']}\n⭐ Stars: {s['total_stars']}\n"
        f"₿ Pending crypto: {s['pending_crypto']}\n\n"
        f"📡 *Traffic sources:*\n{utm_lines}",
        parse_mode="Markdown",
    )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /give <chat_id> <plan> <months>"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("❌ Format: /give 123456 diamond 1")
        return
    try:
        cid    = int(args[0])
        pk     = args[1].lower()
        months = int(args[2])
        assert pk in (*PAID_PLANS, "trial"), f"Unknown plan: {pk}"
        if pk == "trial":
            new_exp = db_apply_trial(cid, 30 * months)
        else:
            new_exp = db_apply_payment(cid, 0, pk, months, "manual")
        await update.message.reply_text(
            f"✅ *{pk}* until {new_exp.strftime('%d.%m.%Y')} for {cid}",
            parse_mode="Markdown",
        )
    except (ValueError, AssertionError) as e:
        await update.message.reply_text(f"❌ {e}\nFormat: /give 123456 diamond 1")


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

    a    = full_analysis(price, _prev_prices.get(pair), pair)
    text = groq_channel_post(a, post_type)
    try:
        sent = await safe_send_photo(context.bot, CHANNEL_ID, cfg["image"], text)
        if not sent:
            await safe_send(context.bot, CHANNEL_ID, text)
        db_save_post(pair, post_type, a["score"], a["ai"].get("sentiment", "?"), price, 0)
        await update.message.reply_text(f"✅ Published! Score={a['score']}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_confirmcrypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /confirmcrypto <code> [tx_hash]"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("❌ Format: /confirmcrypto ABCD1234 tx_hash")
        return
    result = db_confirm_crypto_payment(args[0], args[1] if len(args) > 1 else "")
    if result is None:
        await update.message.reply_text("❌ Pending crypto payment not found.")
        return
    await update.message.reply_text(
        f"✅ Crypto confirmed\n"
        f"User: `{result['chat_id']}`\n"
        f"Plan: *{plan_label(result['plan'])}* x{result['months']} mo\n"
        f"Until: *{result['expires'].strftime('%d.%m.%Y')}*",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════
#  Deep Analysis  (Gemini — admin only)
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
                       macro: str, econ: dict, mode: str) -> str:
    """Build the comprehensive prompt for Gemini."""
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
        econ_text = f"\n⚠️ HIGH-IMPACT USD EVENTS TODAY: {', '.join(econ['events'])}"

    depth = "extremely detailed with specific price levels and probabilities" \
            if mode == "deep" else "detailed and actionable"

    return f"""You are a professional XAU/USD (Gold) trader and market analyst with 15+ years experience.
Your analysis must be {depth}.

═══ CURRENT MARKET DATA ═══
Pair: {cfg['name']}
Current Price: {fmt_price(price, pair)} USD
{econ_text}

═══ MULTI-TIMEFRAME TECHNICAL ANALYSIS ═══
{tf_text}

═══ MACRO & NEWS CONTEXT ═══
{macro}

═══ YOUR TASK ═══
Provide a COMPREHENSIVE trading analysis for XAU/USD covering:

1. **OVERALL BIAS** — Bullish/Bearish/Neutral with confidence % and reasoning

2. **MULTI-TIMEFRAME ALIGNMENT**
   - 5m: short-term momentum
   - 15m: intraday trend
   - 1h: swing direction
   - 4h: dominant trend
   - Are timeframes aligned or conflicting?

3. **KEY LEVELS** (be specific with prices)
   - 3 major resistance levels with explanation
   - 3 major support levels with explanation
   - Most important level to watch RIGHT NOW

4. **TRADE SETUPS** (give 2-3 concrete setups)
   For each setup:
   - Direction: BUY or SELL
   - Entry: exact price or zone
   - Stop Loss: exact price and reasoning
   - Take Profit 1 (conservative): exact price
   - Take Profit 2 (extended): exact price
   - Risk/Reward ratio
   - Timeframe: when to expect the move
   - Trigger: what needs to happen for entry

5. **MACRO IMPACT**
   - How do current news/events affect gold?
   - DXY correlation — dollar strengthening or weakening?
   - Risk sentiment — risk-on or risk-off?
   - What to watch in next 24-48 hours

6. **INVALIDATION**
   - What price level would invalidate the bullish scenario?
   - What price level would invalidate the bearish scenario?

7. **FINAL RECOMMENDATION**
   - Best setup right now with score /100
   - Optimal entry timing
   - Position sizing suggestion (% of capital)

Format your response with clear sections and specific prices throughout.
Be direct and actionable — this is for live trading decisions."""


def _gemini_generate_text(prompt: str, model: str, max_tokens: int) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.25},
    }
    r = requests.post(url, params={"key": GEMINI_KEY}, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Gemini response: {data}") from e


def _gemini_generate_vision(prompt: str, image_b64: str, model: str, max_tokens: int) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            ],
        }],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.25},
    }
    r = requests.post(url, params={"key": GEMINI_KEY}, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Gemini response: {data}") from e


def _gemini_deep_analysis(pair: str, price: float, mode: str) -> str:
    """
    Run deep analysis using Gemini.
    mode: 'fast' or 'deep'
    """
    model = GEMINI_DEEP_MODEL if mode == "deep" else GEMINI_FAST_MODEL

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

    prompt = _build_deep_prompt(pair, price, tf_data, macro, econ, mode)

    return _gemini_generate_text(prompt, model, 2500 if mode == "deep" else 1800)


async def cmd_deepanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /deepanalysis          — Gemini Flash (fast)
      /deepanalysis full     — Gemini Pro (deep)
      /deepanalysis BTCUSD   — different pair
    """
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not GEMINI_KEY:
        await update.message.reply_text(
            "❌ GEMINI\\_KEY not set in .env\n\n"
            "Also supported: GEMINI\\_API\\_KEY or GOOGLE\\_API\\_KEY",
            parse_mode="Markdown",
        )
        return

    args = context.args or []
    mode = "fast"
    pair = "XAUUSD"

    for arg in args:
        if arg.lower() == "full":
            mode = "deep"
        elif arg.upper() in PAIRS:
            pair = arg.upper()

    cfg        = PAIRS[pair]
    model_name = GEMINI_DEEP_MODEL if mode == "deep" else GEMINI_FAST_MODEL
    cost_hint  = "deep" if mode == "deep" else "fast"

    await update.message.reply_text(
        f"🧠 *Deep Analysis* — {cfg['emoji']} {cfg['name']}\n\n"
        f"Model: `{model_name}`  ({cost_hint})\n"
        f"⏳ Gathering data from 4 timeframes + macro news…\n\n"
        f"_This takes 30-60 seconds — please wait_",
        parse_mode="Markdown",
    )

    price = get_price(pair)
    if not price:
        await update.message.reply_text("❌ Could not get current price.")
        return

    try:
        loop   = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _gemini_deep_analysis, pair, price, mode),
            timeout=120,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⏱ Analysis timed out (120s). Try again or use `/deepanalysis` without `full`.",
            parse_mode="Markdown",
        )
        return
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
        log.error("Deep analysis error: %s", e)
        return

    # Split long messages (Telegram limit 4096 chars)
    header = (
        f"🧠 *DEEP ANALYSIS — {cfg['emoji']} {cfg['name']}*\n"
        f"💰 Price: *{fmt_price(price, pair)}*  |  "
        f"Model: `{model_name}`\n"
        f"{'─' * 30}\n\n"
    )
    full_text = header + result

    chunk_size = 3800
    chunks = []
    while len(full_text) > chunk_size:
        # Split at paragraph boundary
        split_at = full_text.rfind("\n\n", 0, chunk_size)
        if split_at == -1:
            split_at = chunk_size
        chunks.append(full_text[:split_at])
        full_text = full_text[split_at:].lstrip()
    if full_text:
        chunks.append(full_text)

    for i, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            # If markdown fails send as plain text
            plain = re.sub(r"[*_`#]", "", chunk)
            await update.message.reply_text(plain)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)

    log.info("Deep analysis: %s %s mode=%s price=%s", pair, model_name, mode, price)


# ═══════════════════════════════════════════════════════════════════
#  Vision Chart Analysis  (Gemini reads screenshot — all users)
# ═══════════════════════════════════════════════════════════════════

async def cmd_chartanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User sends a chart screenshot → Gemini analyses it visually.
    Usage: send photo with caption /chart or just /chart then send photo
    Available to all subscribed users.
    """
    cid = update.effective_chat.id
    acc = db_access(cid)
    if not acc["allowed"] and cid != ADMIN_ID:
        await update.message.reply_text(
            "⛔ Subscribe to use Chart Analysis.\n\n"
            "Tap /start → 💳 Subscription",
        )
        return

    if not GEMINI_KEY:
        await update.message.reply_text("❌ Vision analysis not configured.")
        return

    # Check if message has a photo
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]   # largest size
    elif update.message.reply_to_message and update.message.reply_to_message.photo:
        photo = update.message.reply_to_message.photo[-1]

    if not photo:
        await update.message.reply_text(
            "📸 *How to use Chart Analysis:*\n\n"
            "1. Open your broker/TradingView chart\n"
            "2. Set your timeframe and indicators\n"
            "3. Take a screenshot\n"
            "4. Send the screenshot to this bot\n"
            "   _(caption is optional)_\n\n"
            "Gemini will analyse the chart and give you:\n"
            "• Trend direction and strength\n"
            "• Key support & resistance levels\n"
            "• Entry, SL and TP suggestion\n"
            "• Overall trade recommendation",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "🔍 *Analysing your chart…*\n_Gemini is reading the image — 15-30 seconds_",
        parse_mode="Markdown",
    )

    try:
        # Download photo from Telegram
        photo_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await photo_file.download_as_bytearray()
        import base64
        photo_b64 = base64.standard_b64encode(bytes(photo_bytes)).decode("utf-8")

        # Get current price for context
        price = get_price("XAUUSD")
        price_ctx = f"Current XAU/USD price: {fmt_price(price, 'XAUUSD')}" if price else ""

        # User caption as additional context
        user_note = ""
        if update.message.caption and update.message.caption.strip():
            note = update.message.caption.replace("/chart", "").strip()
            if note:
                user_note = f"\nUser note: {note}"

        prompt = (
            f"You are a professional XAU/USD chart analyst.\n"
            f"{price_ctx}{user_note}\n\n"
            "Analyse this trading chart screenshot and provide:\n\n"
            "1. **CHART OVERVIEW**\n"
            "   - What asset and timeframe do you see?\n"
            "   - What indicators are visible?\n\n"
            "2. **TREND ANALYSIS**\n"
            "   - Current trend direction (up/down/sideways)\n"
            "   - Trend strength and momentum\n"
            "   - Key pattern if visible (breakout, reversal, consolidation)\n\n"
            "3. **KEY LEVELS** (give specific prices if visible on chart)\n"
            "   - Major resistance levels\n"
            "   - Major support levels\n"
            "   - Most important level right now\n\n"
            "4. **TRADE SETUP**\n"
            "   - Direction: BUY or SELL\n"
            "   - Entry zone\n"
            "   - Stop Loss placement and reasoning\n"
            "   - Take Profit 1 (conservative)\n"
            "   - Take Profit 2 (extended)\n"
            "   - Risk/Reward ratio\n\n"
            "5. **RECOMMENDATION**\n"
            "   - Overall verdict: ENTER / WAIT / AVOID\n"
            "   - Confidence: X/100\n"
            "   - Key thing to watch\n\n"
            "Be specific with price levels where visible. "
            "If the chart quality is poor or unclear, say so."
        )

        result = _gemini_generate_vision(prompt, photo_b64, GEMINI_FAST_MODEL, 1200)

        header = "📊 *Chart Analysis*\n" + "─" * 28 + "\n\n"
        full   = header + result

        # Split if too long
        if len(full) <= 4000:
            try:
                await update.message.reply_text(full, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(re.sub(r"[*_`#]", "", full))
        else:
            parts = [full[i:i+3800] for i in range(0, len(full), 3800)]
            for i, part in enumerate(parts):
                try:
                    await update.message.reply_text(part, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(re.sub(r"[*_`#]", "", part))
                if i < len(parts) - 1:
                    await asyncio.sleep(0.3)

        log.info("Chart analysis: cid=%s plan=%s", cid, acc["plan"])

    except Exception as e:
        await update.message.reply_text(
            f"❌ Analysis failed: {str(e)[:150]}\n\nTry again in a moment."
        )
        log.error("Chart analysis error: %s", e)


async def handle_photo(update, context):
    cid = update.effective_chat.id
    acc = db_access(cid)
    if not acc["allowed"] and cid != ADMIN_ID:
        return
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
    q    = update.callback_query
    cid  = q.message.chat_id
    u    = get_user(cid)
    acc  = db_access(cid)
    plan = acc["plan"]
    await q.answer()

    if q.data == "choose_pair":
        await safe_edit(q, "🔀 *Select pair*\n\n🔒 Crypto pairs — Pro or Diamond only",
                        markup=kb_pairs(u.selected_pair, plan))
        return

    if q.data.startswith("pair_"):
        new_pair = q.data[5:]
        cfg = PAIRS.get(new_pair)
        if not cfg:
            await safe_edit(q, "❌ Unknown pair.", markup=kb_main(plan, u.selected_pair))
            return
        if plan not in cfg["plans"]:
            await safe_edit(q, f"🔒 *{cfg['name']} — Pro or Diamond only*", markup=kb_sub())
            return
        u.selected_pair = new_pair
        price = get_price(new_pair)
        await safe_edit(
            q,
            f"✅ *{cfg['emoji']} {cfg['name']}*\n\n"
            f"Price: *{fmt_price(price, new_pair) if price else 'N/A'}*",
            markup=kb_main(plan, new_pair),
        )
        return

    if q.data == "back_main":
        await safe_edit(q, "🤖 *AI Trading Bot*", markup=kb_main(plan, u.selected_pair))
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

    if q.data == "crypto_menu":
        if not _crypto_wallet_lines():
            await safe_edit(q, "❌ Direct crypto payment is not configured yet.", markup=kb_sub())
            return
        await safe_edit(
            q,
            "₿ *Direct crypto payment*\n\nChoose a plan, send payment to one of the configured wallets, then admin confirms it.",
            markup=kb_crypto_plans(),
        )
        return

    if q.data.startswith("crypto_"):
        try:
            _, pk, months_raw = q.data.split("_", 2)
            months = int(months_raw)
            pending = db_create_crypto_payment(cid, pk, months)
        except (ValueError, sqlite3.IntegrityError) as e:
            await safe_edit(q, f"❌ Crypto payment error: {e}", markup=kb_crypto_plans())
            return
        wallets = "\n".join(_crypto_wallet_lines())
        await safe_edit(
            q,
            f"₿ *Direct crypto payment*\n\n"
            f"Plan: *{plan_label(pk)}* x{months} mo\n"
            f"Amount: *${pending['amount_usd']}* equivalent\n"
            f"Payment code: `{pending['code']}`\n\n"
            f"{wallets}\n\n"
            f"After sending, include this code with your transaction proof. "
            f"Admin confirms with `/confirmcrypto {pending['code']} <tx_hash>`.",
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="crypto_menu")]]),
        )
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"₿ *Pending crypto payment*\n"
                f"Code: `{pending['code']}`\nUser: `{cid}`\n"
                f"Plan: *{plan_label(pk)}* x{months} mo\nAmount: *${pending['amount_usd']}*",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Crypto pending admin notify failed: %s", e)
        return

    buy_map = {
        f"buy_{plan}_{months}": (
            plan, months, stars, f"{plan_label(plan)} — {months} month{'s' if months > 1 else ''}"
        )
        for (plan, months), stars in PLAN_PRICES_STARS.items()
    }
    if q.data in buy_map:
        pk, months, stars, title = buy_map[q.data]
        desc = PLAN_DESCRIPTIONS[pk]
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
                loop.run_in_executor(None, full_analysis, price_val, _prev_prices.get(pair), pair),
                timeout=45,   # increased: parallel fetch ~15s + groq ~20s
            )
        except asyncio.TimeoutError:
            await safe_edit(q,
                "⏱ *Analysis timed out.*\n\n"
                "_The server took too long. This usually happens once — please try again._",
                markup=kb_main(plan, pair),
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
            await safe_edit(q, "❌ Price error.", markup=kb_main(plan, pair))
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
            await safe_edit(q, "❌ Error.", markup=kb_main(plan, pair))
            return
        ps.waiting_entry_price = opt
        ps.persist(cid, pair)
        await safe_edit(q, f"⏳ Waiting for *{fmt_price(opt, pair)}*",
                        markup=kb_main(plan, pair))
        return

    if q.data == "cancel":
        u.pending_analysis = None
        await safe_edit(q, "↩️ Cancelled", markup=kb_main(plan, pair))
        return

    if q.data == "stop":
        ps.running = False
        ps.persist(cid, pair)
        await safe_edit(q, "⏹ Stopped", markup=kb_main(plan, pair))
        return

    if q.data == "reset":
        ps.reset(cid, pair)
        u.pending_analysis = None
        await safe_edit(q, "🔄 *Reset*", markup=kb_main(plan, pair))
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
        await safe_edit(q, f"📊 *Status*\n\n{msg}", markup=kb_main(plan, u.selected_pair))
        return

    # ── Signal accuracy stats ────────────────────────────────────
    if q.data.startswith("stats_"):
        await _handle_stats_callback(q, cid, q.data)
        return

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    parsed = _parse_plan_payload(q.invoice_payload)
    if parsed is None:
        await q.answer(ok=False, error_message="Invalid subscription payload.")
        return
    _pk, _months, expected_stars = parsed
    if q.currency != "XTR" or q.total_amount != expected_stars:
        await q.answer(ok=False, error_message="Invalid subscription amount.")
        return
    await q.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid     = update.effective_chat.id
    payment = update.message.successful_payment
    stars   = payment.total_amount
    payload = payment.invoice_payload
    charge  = payment.telegram_payment_charge_id
    parsed = _parse_plan_payload(payload)
    if parsed is None:
        log.error("Invalid payment payload: %s", payload)
        await update.message.reply_text("❌ Payment payload invalid. Contact admin.")
        return
    pk, months, expected_stars = parsed
    if payment.currency != "XTR" or stars != expected_stars:
        log.error("Invalid payment amount: payload=%s currency=%s stars=%s", payload, payment.currency, stars)
        await update.message.reply_text("❌ Payment amount invalid. Contact admin.")
        return
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

        now_utc = datetime.utcnow()
        h = now_utc.hour

        # 2) Market analysis posts (with image)
        if h in CHANNEL_HOURS_UTC and h != _last_channel_post_hour:
            _last_channel_post_hour = h
            for pair in PAIRS:
                price = _prices.get(pair)
                if not price:
                    continue
                try:
                    a    = full_analysis(price, _prev_prices.get(pair), pair)
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

        # 3) Article posts (with image)
        if h in ARTICLE_HOURS_UTC and h != _last_article_hour:
            _last_article_hour = h
            topic_type = "edu" if _article_index % 2 == 0 else "news"
            topic = (EDU_TOPICS[(_article_index // 2) % len(EDU_TOPICS)]
                     if topic_type == "edu" else random.choice(NEWS_TOPICS))
            _article_index += 1
            try:
                body = groq_article(topic_type, topic)
                text = format_article_post(topic_type, body)
                await send_article_with_image(context.bot, CHANNEL_ID, topic_type, topic, text)
                log.info("Channel: article [%s] published: %s", topic_type, topic[:50])
            except Exception as e:
                log.error("Article post error: %s", e)

        # 4) Per-user trade monitoring + auto-signals
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

                if (plan in AUTO_SIGNAL_PLANS
                        and not ps.has_trade and not ps.is_waiting
                        and time.time() - ps.last_signal_time > AUTO_COOLDOWN):
                    prev = _prev_prices.get(pair)
                    if prev:
                        a = full_analysis(price, prev, pair)
                        if a["score"] >= 75 and a["score"] > ps.last_signal_score:
                            text = (build_analysis_text(a)
                                    + f"\n\n📡 *Auto-signal!* Score: *{a['score']}/100*")
                            await safe_send(context.bot, cid, text,
                                            reply_markup=kb_main(plan, pair))
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
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
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
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
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
                age_h = (datetime.utcnow() - start).total_seconds() / 3600
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

    # Notify auto-signal subscribers
    notified = 0
    for cid, u in list(USERS.items()):
        acc = db_access(cid)
        if acc["plan"] in AUTO_SIGNAL_PLANS:
            try:
                await safe_send(
                    app_ref.bot, cid,
                    f"⚡ *New TradingView Signal!*\n\n{text}",
                    reply_markup=kb_main(acc["plan"], pair),
                )
                notified += 1
            except Exception:
                pass

    log.info("TV webhook: %s %s @ %s — signal #%d, notified %d Pro users",
             direction, pair, entry, sig_id, notified)
    return web.Response(text=f"ok signal_id={sig_id}")


# Global reference to PTB Application for webhook handler
_APP_REF = None


def _get_app_ref():
    return _APP_REF


async def _start_webhook_server() -> None:
    """Start aiohttp webhook server. Only starts if TV_WEBHOOK_SECRET is set in .env"""
    if not TV_WEBHOOK_SECRET or TV_WEBHOOK_SECRET == "change_this_secret_123":
        log.info("TradingView webhook disabled (TV_WEBHOOK_SECRET not configured)")
        return
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not installed — TradingView webhook disabled.")
        return

    app_web = web.Application()
    app_web.router.add_post("/tv", _tv_webhook_handler)

    async def health(request):
        return web.Response(text="ok")
    app_web.router.add_get("/health", health)

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", TV_WEBHOOK_PORT)
    await site.start()
    log.info("✅ TradingView webhook listening on port %d", TV_WEBHOOK_PORT)


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
    topic = (random.choice(NEWS_TOPICS) if topic_type == "news" else EDU_TOPICS[_article_index % len(EDU_TOPICS)])
    if topic_type == "edu":
        _article_index += 1
    try:
        body = groq_article(topic_type, topic)
        text = format_article_post(topic_type, body)
        await send_article_with_image(context.bot, CHANNEL_ID, topic_type, topic, text)
        await update.message.reply_text(f"Published! Type: {topic_type} Topic: {topic[:60]}")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:120]}")


def main() -> None:
    global _APP_REF
    db_init()
    log.info("DB initialised. Starting bot…")

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
    app.add_handler(CommandHandler("confirmcrypto", cmd_confirmcrypto))
    app.add_handler(CommandHandler("forcepost",    cmd_forcepost))
    app.add_handler(CommandHandler("forcearticle", cmd_forcearticle))
    app.add_handler(CommandHandler("welcome",      cmd_welcome))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.job_queue.run_repeating(monitor, interval=60, first=15)

    # Start TradingView webhook server
    async def post_init(application):
        await _start_webhook_server()

    app.post_init = post_init

    log.info("✅ Bot running… Stop with Ctrl+C")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
