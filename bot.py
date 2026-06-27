
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
Env: Groq/OpenRouter pools/Gemini; `AI_ROUTE_*`; OpenRouter pools; Gemini `GEMINI_DISABLE_THINKING` (see .env.example).
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
from zoneinfo import ZoneInfo
from textwrap import dedent

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
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
# OpenRouter — role-based key pools (light vs heavy workloads). See OPENROUTER_KEYS_* and AI_ROUTE_*.
OPENROUTER_API_KEY     = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_KEY_2   = os.getenv("OPENROUTER_API_KEY_2", "").strip()
OPENROUTER_API_KEYS    = os.getenv("OPENROUTER_API_KEYS", "").strip()  # optional comma-separated extra keys
OPENROUTER_MODEL       = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "")  # if empty, uses OPENROUTER_MODEL
OPENROUTER_SITE_URL    = os.getenv("OPENROUTER_SITE_URL", "")   # optional HTTP-Referer for OpenRouter rankings
OPENROUTER_APP_TITLE   = os.getenv("OPENROUTER_APP_TITLE", "Gold Crypto Trading Bot")
OPENROUTER_API_URL     = "https://openrouter.ai/api/v1/chat/completions"
try:
    _or402_hold = int(os.getenv("OPENROUTER_402_CREDIT_HOLD_SEC", "3600"))
except ValueError:
    _or402_hold = 3600
OPENROUTER_402_CREDIT_HOLD_SEC = max(60, _or402_hold)
# Optional explicit pools (comma-separated API keys). Defaults use OPENROUTER_API_KEY / _2.
OPENROUTER_KEYS_LIGHT = os.getenv("OPENROUTER_KEYS_LIGHT", "").strip()
OPENROUTER_KEYS_HEAVY = os.getenv("OPENROUTER_KEYS_HEAVY", "").strip()
# Конвенція два ключі без явних OPENROUTER_KEYS_*:
#   OPENROUTER_API_KEY  — першим (типово оплачений акаунт)
#   OPENROUTER_API_KEY_2 — після failover першого (типово «безкоштовний» / менший ліміт)

_openrouter_rr_lock = threading.Lock()
# Per-pool round-robin cursor (light / heavy / merged).
_openrouter_pool_rr: dict[str, int] = {"light": 0, "heavy": 0, "merged": 0}
# API key string → time.monotonic() until deprioritised (HTTP 402 insufficient credits).
_OPENROUTER_CREDIT_HOLD_UNTIL: dict[str, float] = {}


def _openrouter_legacy_extra_keys() -> list[str]:
    out: list[str] = []
    for part in OPENROUTER_API_KEYS.split(","):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def _dedupe_api_keys(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _openrouter_keys_light() -> list[str]:
    """
    Короткі запити: спочатку OPENROUTER_API_KEY (paid), далі OPENROUTER_API_KEYS*, останній
    fallback — OPENROUTER_API_KEY_2 (free/reserve), без дублікатів.
    """
    if OPENROUTER_KEYS_LIGHT:
        return _dedupe_api_keys(
            [p.strip() for p in OPENROUTER_KEYS_LIGHT.split(",") if p.strip()]
        )
    keys: list[str] = []
    if OPENROUTER_API_KEY:
        keys.append(OPENROUTER_API_KEY)
    keys.extend(k for k in _openrouter_legacy_extra_keys() if k not in keys)
    if OPENROUTER_API_KEY_2 and OPENROUTER_API_KEY_2 not in keys:
        keys.append(OPENROUTER_API_KEY_2)
    return _dedupe_api_keys(keys)


def _openrouter_keys_heavy() -> list[str]:
    """Long-form + vision on OpenRouter: deep analysis, chart screenshots (openrouter_heavy)."""
    if OPENROUTER_KEYS_HEAVY:
        return _dedupe_api_keys(
            [p.strip() for p in OPENROUTER_KEYS_HEAVY.split(",") if p.strip()]
        )
    duo = _dedupe_api_keys(
        [k for k in (OPENROUTER_API_KEY, OPENROUTER_API_KEY_2) if k]
    )
    if len(duo) >= 2:
        # OPENROUTER_API_KEY (перший у .env) → OPENROUTER_API_KEY_2
        return duo
    if len(duo) == 1:
        # One explicit key configured — reuse the light/auxiliary pool for extra keys without dropping them.
        out = duo.copy()
        for k in _openrouter_keys_light():
            if k not in out:
                out.append(k)
        return out
    return _openrouter_keys_light()


def _openrouter_keys_merged() -> list[str]:
    out: list[str] = []
    for k in _openrouter_keys_light() + _openrouter_keys_heavy():
        if k not in out:
            out.append(k)
    return out


def _openrouter_configured() -> bool:
    return bool(_openrouter_keys_merged())
# Google Gemini — deep analysis, chart vision, Groq 429 fallback (works alongside OpenRouter)
GEMINI_KEY   = os.getenv("GEMINI_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
try:
    _deep_out_cap = int(os.getenv("DEEP_ANALYSIS_MAX_OUTPUT_TOKENS", "8192").strip())
except ValueError:
    _deep_out_cap = 8192
# Long Deep Analysis replies need a high ceiling; ~1800 output tokens truncates mid-paragraph (MAX_TOKENS).
DEEP_ANALYSIS_MAX_OUTPUT_TOKENS = max(2048, min(_deep_out_cap, 65536))
try:
    _cv_out_cap = int(os.getenv("CHART_VISION_MAX_OUTPUT_TOKENS", "4096").strip())
except ValueError:
    _cv_out_cap = 4096
# Chart screenshot multimodal replies; tight limits cut off before SL/TP/verdict sections.
CHART_VISION_MAX_OUTPUT_TOKENS = max(1024, min(_cv_out_cap, 65536))

# OpenRouter: separate output caps — balance is reserved using *requested* max_tokens, so a huge
# DEEP_ANALYSIS_MAX_OUTPUT_TOKENS can HTTP 402 on low credits even if the reply is short.
try:
    _or_aff_margin = int(os.getenv("OPENROUTER_AFFORD_MARGIN", "32").strip())
except ValueError:
    _or_aff_margin = 32
OPENROUTER_AFFORD_MARGIN = max(8, min(_or_aff_margin, 512))

try:
    _or_out_floor = int(os.getenv("OPENROUTER_OUTPUT_FLOOR", "96").strip())
except ValueError:
    _or_out_floor = 96
OPENROUTER_OUTPUT_FLOOR = max(32, min(_or_out_floor, 4096))

try:
    _or_deep_cap = int(os.getenv("OPENROUTER_DEEP_MAX_TOKENS", "2048").strip())
except ValueError:
    _or_deep_cap = 2048
OPENROUTER_DEEP_MAX_OUTPUT_TOKENS = max(
    OPENROUTER_OUTPUT_FLOOR + 32, min(_or_deep_cap, 65536)
)

try:
    _or_chart_cap = int(os.getenv("OPENROUTER_CHART_MAX_TOKENS", "2048").strip())
except ValueError:
    _or_chart_cap = 2048
OPENROUTER_CHART_MAX_OUTPUT_TOKENS = max(
    OPENROUTER_OUTPUT_FLOOR + 32, min(_or_chart_cap, 65536)
)

# Last line of defence: clip every OpenRouter completion request unless explicitly raised via .env.
# Prevents stray 8192 max_tokens burns when upstream env vars are misconfigured or an old compose file pins them high.
try:
    _ohard = int(os.getenv("OPENROUTER_HARD_OUTPUT_CAP", "1536").strip())
except ValueError:
    _ohard = 1536
OPENROUTER_HARD_OUTPUT_CAP = max(
    OPENROUTER_OUTPUT_FLOOR + 64, min(_ohard, 65536)
)


def openrouter_deep_effective_output_cap() -> int:
    """OpenRouter Deep Analysis ceiling (does not shrink Gemini budgets)."""
    return min(
        DEEP_ANALYSIS_MAX_OUTPUT_TOKENS,
        OPENROUTER_DEEP_MAX_OUTPUT_TOKENS,
        OPENROUTER_HARD_OUTPUT_CAP,
    )


def openrouter_chart_effective_output_cap() -> int:
    return min(
        CHART_VISION_MAX_OUTPUT_TOKENS,
        OPENROUTER_CHART_MAX_OUTPUT_TOKENS,
        OPENROUTER_HARD_OUTPUT_CAP,
    )
def _gemini_thinking_kw() -> dict:
    """Prefer disabling internal reasoning so visible output uses the token budget."""
    import google.genai.types as gtypes

    v = os.getenv("GEMINI_DISABLE_THINKING", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return {}
    try:
        return {"thinking_config": gtypes.ThinkingConfig(thinking_budget=0)}
    except TypeError:
        # Gemini 3.x SDKs may replace thinking_budget with thinking_level.
        try:
            return {"thinking_config": gtypes.ThinkingConfig(thinking_level="LOW")}
        except TypeError:
            return {}


def _gemini_candidate_visible_text(candidate) -> str:
    chunks: list[str] = []
    content = getattr(candidate, "content", None)
    for part in getattr(content, "parts", None) or []:
        t = getattr(part, "text", None)
        if isinstance(t, str) and t.strip():
            chunks.append(t)
    return "".join(chunks).strip()


def _gemini_response_visible_text(response, *, context: str) -> str:
    """Safely extract user-visible text (handles blocked prompts and quirky responses)."""
    import google.genai.types as gtypes

    fb = getattr(response, "prompt_feedback", None)
    block_reason = getattr(fb, "block_reason", None) if fb else None
    if block_reason:
        raise RuntimeError(
            f"Gemini blocked the request [{context}]: {block_reason}",
        )

    extracted: list[str] = []
    try:
        t = response.text  # raises if mixed/blocked/no parts depending on SDK
        if isinstance(t, str) and t.strip():
            extracted.append(t.strip())
    except Exception as e:
        log.info("Gemini %s: response.text not used (%s)", context, str(e)[:200])

    if not extracted:
        for cand in getattr(response, "candidates", None) or []:
            s = _gemini_candidate_visible_text(cand)
            if s:
                extracted.append(s)

    out = extracted[0] if len(extracted) == 1 else "\n".join(extracted).strip()

    fr = None
    cands = getattr(response, "candidates", None) or []
    if cands:
        fr = getattr(cands[0], "finish_reason", None)
        if fr == gtypes.FinishReason.MAX_TOKENS:
            suffix = ""
            if context == "deep_analysis":
                suffix = (
                    " — raise DEEP_ANALYSIS_MAX_OUTPUT_TOKENS "
                    "or ensure GEMINI_DISABLE_THINKING=1 (thinking eats output quota)"
                )
            elif context == "chart_vision":
                suffix = (
                    " — raise CHART_VISION_MAX_OUTPUT_TOKENS "
                    "or ensure GEMINI_DISABLE_THINKING=1"
                )
            log.warning("Gemini %s hit MAX_TOKENS%s", context, suffix)
        unsafe = frozenset(
            {
                gtypes.FinishReason.SAFETY,
                gtypes.FinishReason.PROHIBITED_CONTENT,
                gtypes.FinishReason.RECITATION,
            }
        )
        if fr in unsafe and not out.strip():
            raise RuntimeError(f"Gemini refused output [{context}]: finish_reason={fr}")

    if not out.strip():
        raise RuntimeError(f"Gemini returned empty visible text [{context}] finish_reason={fr}")
    return out


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
def _parse_admin_id(raw: str | None) -> int:
    """Robust ADMIN_ID parsing (quotes / whitespace from .env)."""
    s = (raw or "").strip().strip('"').strip("'").split("#", 1)[0].strip()
    try:
        return int(s or "123456789", 10)
    except ValueError:
        log.error("ADMIN_ID invalid %r — using fallback 123456789", raw)
        return 123456789


ADMIN_ID = _parse_admin_id(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID   = os.getenv("CHANNEL_ID",  "@your_channel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@your_bot")


def bot_telegram_url() -> str:
    """Canonical https://t.me/… link (bare @username in channels is not always clickable)."""
    u = (BOT_USERNAME or "").strip()
    if u.startswith("@"):
        u = u[1:]
    if not u or u in ("your_bot", ""):
        u = "your_bot"
    return f"https://t.me/{u}"


def bot_link_markdown() -> str:
    """Telegram Markdown: explicit link so channel captions stay tappable."""
    return f"[{BOT_USERNAME}]({bot_telegram_url()})"


def bot_link_html() -> str:
    """HTML <a href> for parse_mode=HTML channel posts."""
    return f'<a href="{bot_telegram_url()}">{BOT_USERNAME}</a>'


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
XAU_VOLATILITY_THRESHOLD = float(os.getenv("XAU_VOLATILITY_THRESHOLD", "0.20"))
ADMIN_MODEL              = os.getenv("ADMIN_MODEL", "qwen/qwen-2.5-72b-instruct")
ADMIN_MACRO_MODEL        = os.getenv("ADMIN_MACRO_MODEL", "perplexity/sonar")

# Long-form deep analysis & chart vision: see DEEP_ANALYSIS_PROVIDER / CHART_VISION_PROVIDER (default: gemini).

TRIAL_DAYS        = int(os.getenv("TRIAL_DAYS", "3") or "3") or 3
TRIAL_DAYS        = max(1, min(TRIAL_DAYS, 14))


def _trial_duration_ua() -> str:
    """Ukrainian 'N days' for fixed marketing copy (trial length)."""
    n = TRIAL_DAYS
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дні"
    return f"{n} днів"
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
# NOWPayments: Basic (incl. 3mo crypto) violates fair price vs gateway minimum — crypto only Pro 3mo + Diamond.
CRYPTO_PAY_ALLOWED = frozenset({("pro", 3), ("diamond", 1), ("diamond", 3)})
DB_PATH           = "users.db"
# Planned channel *articles* (LLM body) — still wall-clock UTC here.
ARTICLE_HOURS_UTC = [8, 14, 20]   # article posts — separate from analysis
# Planned channel market analysis: години 09–18 у зоні CHANNEL_ANALYSIS_TZ (за замовчуванням UTC) — див. константи після PAIRS.
# Background job interval (seconds). Scheduled channel/article LLM fires when the calendar hour rolls.
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
    "TONUSD": {
        "name": "TON/USD", "emoji": "🔹", "yahoo": "TON11419-USD", "stooq": "tonusd",
        "news_q": "Toncoin TON Telegram Open Network blockchain",
        "sl_pct": 4.5, "tp_pct": 7.5,
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

# Канальні огляди: 1 пара на календарну годину, стартова година 09:00 у зоні CHANNEL_ANALYSIS_TZ, 10 годин поспіль.
# Часова зона за замовчуванням UTC (без прив’язки до міста); інша — через .env CHANNEL_ANALYSIS_TZ=… або правка рядка нижче.
CHANNEL_ANALYSIS_TZ_NAME = (os.getenv("CHANNEL_ANALYSIS_TZ", "").strip() or "UTC")
CHANNEL_ANALYSIS_LOCAL_START_HOUR = 9
CHANNEL_ANALYSIS_HOURLY_SLOTS = 10  # години включно від START до START+SLOTS-1


def channel_scheduled_analysis_pairs() -> list[str]:
    """
    Порядок пар для черговості слотів 0…N−1 під час годинникового каналу.

    Якщо CHANNEL_ANALYSIS_PAIRS у `.env` заданий — лише він (доречно з телефона лишити порожнім).
    Інакше — усі ключі PAIRS у стабільному порядку (золото першим тощо).
    """
    raw = os.getenv("CHANNEL_ANALYSIS_PAIRS", "").strip()
    if not raw:
        return list(PAIRS.keys())
    picked: list[str] = []
    for part in raw.split(","):
        pid = part.strip().upper()
        if pid in PAIRS and pid not in picked:
            picked.append(pid)
    if not picked:
        log.warning(
            "CHANNEL_ANALYSIS_PAIRS has no valid ids (valid: %s) — using all pairs",
            ",".join(PAIRS.keys()),
        )
        return list(PAIRS.keys())
    return picked


def channel_articles_enabled() -> bool:
    """Edu/news за ARTICLE_HOURS_UTC. За замовчуванням вимкнено — у `.env` лише ключі; увімкнути явно CHANNEL_ARTICLES_ENABLED=1."""
    v = os.getenv("CHANNEL_ARTICLES_ENABLED", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def channel_analysis_local_datetime() -> datetime:
    """Поточний час у зоні CHANNEL_ANALYSIS_TZ_NAME (розклад годинникових постів каналу)."""
    try:
        return datetime.now(ZoneInfo(CHANNEL_ANALYSIS_TZ_NAME))
    except Exception:
        log.warning(
            "%r timezone unavailable — using UTC for channel hourly schedule",
            CHANNEL_ANALYSIS_TZ_NAME,
        )
        return datetime.now(UTC)


def post_type_for_channel_local_hour(local_hour: int) -> str:
    """Morning/midday/evening підпис якщо промпт його використовує."""
    if local_hour < 12:
        return "morning"
    if local_hour < 16:
        return "midday"
    return "evening"


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


def _migrate_users_analytics_columns(c: sqlite3.Connection) -> None:
    """Add language_code / is_premium / ref_rewards_received for existing SQLite DBs (CREATE only covers fresh installs)."""
    cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    if "language_code" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN language_code TEXT")
    if "is_premium" not in cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0"
        )
    if "ref_rewards_received" not in cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN ref_rewards_received INTEGER DEFAULT 0"
        )


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
                last_active      TEXT,
                language_code    TEXT,
                is_premium       INTEGER NOT NULL DEFAULT 0,
                ref_rewards_received INTEGER DEFAULT 0
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

        _migrate_users_analytics_columns(c)


def db_upsert_user(
    cid: int,
    username: str = "",
    fname: str = "",
    *,
    language_code: str | None = None,
    is_premium: bool | None = None,
) -> bool:
    """
    Upsert profile + bump last_active. Returns True if this call inserted a brand-new row.
    Passing language_code/is_premium as None skips overwriting those columns on UPDATE.
    """

    def _lang_norm(raw: str | None) -> str:
        z = (raw or "").strip().lower()
        if z.startswith("uk") or z.startswith("ua"):
            return "uk"
        return "en"

    with db_connect() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id=?", (cid,)).fetchone()
        if row is None:
            trial_ends = (datetime.now(UTC) + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
            lang_ins = _lang_norm(language_code)
            prem_ins = 1 if bool(is_premium if is_premium is not None else False) else 0

            c.execute(
                "INSERT INTO users(chat_id,username,first_name,plan,trial_ends,last_active,"
                "language_code,is_premium) "
                "VALUES(?,?,?,'trial',?,datetime('now'),?,?)",
                (cid, username, fname, trial_ends, lang_ins, prem_ins),
            )
            return True

        nu = username if username else (row["username"] or "")
        nf = fname if fname else (row["first_name"] or "")
        # Keep user's custom language preference if it exists in the database
        nlang = row["language_code"] if row["language_code"] else _lang_norm(language_code)
        if is_premium is not None:
            npm = 1 if is_premium else 0
        else:
            npm = int(row["is_premium"] or 0)

        c.execute(
            "UPDATE users SET last_active=datetime('now'),username=?,first_name=?,"
            "language_code=?,is_premium=? WHERE chat_id=?",
            (nu, nf, nlang, npm, cid),
        )
        return False


def db_get_user_lang(cid: int) -> str:
    """Fetch user's normalized language ('uk' or 'en')."""
    try:
        with db_connect() as c:
            row = c.execute("SELECT language_code FROM users WHERE chat_id=?", (cid,)).fetchone()
        if row and row["language_code"] in ("uk", "en"):
            return row["language_code"]
    except Exception:
        pass
    return "en"


def db_set_user_lang(cid: int, lang: str) -> None:
    """Set user's language ('uk' or 'en')."""
    clean_lang = "uk" if lang == "uk" else "en"
    try:
        with db_connect() as c:
            c.execute("UPDATE users SET language_code=? WHERE chat_id=?", (clean_lang, cid))
    except Exception as e:
        log.error("Failed to set user lang: %s", e)


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
    
    try:
        with db_connect() as c:
            ref_row = c.execute(
                "SELECT referrer_id, bonus_given FROM referrals WHERE referred_id=?",
                (cid,),
            ).fetchone()
        if ref_row and not ref_row["bonus_given"]:
            referrer_id = ref_row["referrer_id"]
            bonus = db_give_referral_bonus(referrer_id, cid)
            if bonus > 0:
                log.info("Referral bonus awarded: %d days given to %s for referring %s", bonus, referrer_id, cid)
                app_ref = _get_app_ref()
                if app_ref is not None:
                    asyncio.create_task(
                        safe_send(
                            app_ref.bot,
                            referrer_id,
                            f"🎁 *+{bonus} days added to your plan!*\n\n"
                            f"Your referred friend just activated a subscription package. 🚀\n\n"
                            f"/refer — see your referral stats",
                        )
                    )
    except Exception as e:
        log.warning("Failed to process referral bonus on payment: %s", e)

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


def db_analytics_report() -> str:
    """Short product analytics snapshot (SQLite last_active timestamps, server TZ)."""
    with db_connect() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        dau = c.execute(
            "SELECT COUNT(*) FROM users WHERE last_active IS NOT NULL "
            "AND datetime(last_active) >= datetime('now', '-1 day')"
        ).fetchone()[0]
        mau = c.execute(
            "SELECT COUNT(*) FROM users WHERE last_active IS NOT NULL "
            "AND datetime(last_active) >= datetime('now', '-30 days')"
        ).fetchone()[0]
        tg_prem = c.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(is_premium, 0)=1",
        ).fetchone()[0]
        rows = c.execute(
            "SELECT COALESCE(language_code, 'unset') AS lng, COUNT(*) AS n FROM users "
            "GROUP BY lng ORDER BY n DESC LIMIT 8"
        ).fetchall()

    langs = ", ".join(f"{r['lng']}: {r['n']}" for r in rows) or "(немає даних)"
    pct_prem = (100.0 * tg_prem / total) if total else 0.0
    # Plain text (no Markdown) so Telegram never drops the reply on parse errors.
    return (
        "📊 Аналітика бота\n\n"
        f"👥 Усього користувачів: {total}\n"
        f"🔥 DAU (~24 год, server local time): {dau}\n"
        f"📈 MAU (~30 д): {mau}\n\n"
        f"💎 Telegram Premium: {tg_prem} (~{pct_prem:.1f}%)\n"
        f"🌍 Топ мов: {langs}\n\n"
        "DAU/MAU беруться з last_active після активності у приватних чатах."
    )


def _fmt_sqlite_ts(dt: str | None, *, with_time: bool) -> str:
    if dt is None or not str(dt).strip():
        return "—"
    s = str(dt).strip().replace("T", " ")
    parts = s.split()
    if not parts:
        return "—"
    day = parts[0][:10]
    if not with_time:
        return day
    if len(parts) < 2:
        return day
    t = parts[1]
    if "." in t:
        t = t.split(".", 1)[0]
    seg = t.split(":")
    return f"{day} {seg[0]}:{seg[1]}" if len(seg) >= 2 else f"{day} {t}"


def db_recent_users_rows(limit: int = 20) -> list[sqlite3.Row]:
    lim = max(1, min(int(limit), 100))
    with db_connect() as c:
        return list(
            c.execute(
                "SELECT chat_id, username, first_name, plan, "
                "COALESCE(is_premium, 0) AS tg_premium, "
                "COALESCE(language_code, '') AS language_code, "
                "joined_at, last_active "
                "FROM users ORDER BY datetime(COALESCE(joined_at, '1970-01-01')) DESC "
                "LIMIT ?",
                (lim,),
            ).fetchall()
        )


def _split_telegram_text(text: str, max_len: int = 3900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    return chunks


def db_admin_users_report(limit: int = 20) -> list[str]:
    rows = db_recent_users_rows(limit=limit)
    if not rows:
        return ["📭 База користувачів порожня."]
    hdr = f"📋 Останні {len(rows)} користувачів (joined_at ↓):\n"
    blocks: list[str] = []
    for i, r in enumerate(rows, 1):
        cid = int(r["chat_id"])
        fn = (r["first_name"] or "").strip()
        un = (r["username"] or "").strip()
        plan = r["plan"] or ""
        prem = bool(int(r["tg_premium"]))
        lg = (r["language_code"] or "").strip() or "—"
        handle = "@" + un if un else f"id {cid}"
        prem_txt = " 💎Premium" if prem else ""
        blocks.append(
            f"{i}. {fn}{prem_txt}\n"
            f"   {handle} • plan {plan} • lang {lg}\n"
            f"   chat_id={cid}\n"
            f"   joined {_fmt_sqlite_ts(r['joined_at'], with_time=False)} • "
            f"last {_fmt_sqlite_ts(r['last_active'], with_time=True)}"
        )
    body = hdr + "\n\n" + "\n\n".join(blocks)
    return _split_telegram_text(body)


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


async def check_channel_subscription(bot, user_id: int) -> bool:
    """Check if user is subscribed to the official Telegram channel."""
    if user_id == ADMIN_ID:
        return True
    if not CHANNEL_ID or CHANNEL_ID == "@your_channel":
        return True
    try:
        chat_target = CHANNEL_ID
        if isinstance(CHANNEL_ID, str) and (CHANNEL_ID.startswith("-") or CHANNEL_ID.isdigit()):
            chat_target = int(CHANNEL_ID)
        member = await bot.get_chat_member(chat_id=chat_target, user_id=user_id)
        if member.status in ("creator", "administrator", "member"):
            return True
        return False
    except Exception as e:
        log.warning("Subscription check failed for user %s: %s", user_id, e)
        return True


async def check_subscription_and_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Send blocker message if user is not subscribed. Returns False."""
    chat = update.effective_chat
    user = update.effective_user
    if user is None or chat is None or chat.type != "private":
        return True
    cid = chat.id
    if cid == ADMIN_ID:
        return True
    is_subbed = await check_channel_subscription(context.bot, cid)
    if is_subbed:
        return True

    channel_url = f"https://t.me/{CHANNEL_ID.lstrip('@')}" if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith("@") else "https://t.me/your_channel"
    lang = db_get_user_lang(cid)
    if lang == "en":
        btn_join = "👉 Subscribe to Channel"
        btn_check = "Check subscription 🔄"
        text = (
            "❌ *Access Restricted!*\n\n"
            "To use this bot, receive signals, and use AI, "
            "you must be subscribed to our official Telegram channel.\n\n"
            "Please subscribe using the link below and click the verification button."
        )
    else:
        btn_join = "👉 Підписатися на канал"
        btn_check = "Перевірити підписку 🔄"
        text = (
            "❌ *Доступ обмежено!*\n\n"
            "Щоб використовувати цього бота, отримувати сигнали та використовувати ШІ, "
            "ви повинні бути підписані на наш офіційний Telegram-канал.\n\n"
            "Будь ласка, підпишіться за посиланням нижче та натисніть кнопку перевірки."
        )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_join, url=channel_url)],
        [InlineKeyboardButton(btn_check, callback_data="check_subscription_refresh")]
    ])
    if update.callback_query:
        try:
            await safe_edit(update.callback_query, text, markup=keyboard)
        except Exception:
            try:
                await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except Exception:
                pass
    else:
        try:
            await safe_send(context.bot, cid, text, reply_markup=keyboard)
        except Exception:
            pass
    return False


async def try_award_referral_bonus(bot, referred_id: int) -> None:
    """Award referral bonus to referrer if referred user is subscribed and has a username."""
    is_subbed = await check_channel_subscription(bot, referred_id)
    if not is_subbed:
        return
    with db_connect() as c:
        row = c.execute(
            "SELECT id, referrer_id, bonus_given FROM referrals WHERE referred_id=?",
            (referred_id,)
        ).fetchone()
    if row and not row["bonus_given"]:
        referrer_id = row["referrer_id"]
        with db_connect() as c:
            referee_row = c.execute("SELECT username FROM users WHERE chat_id=?", (referred_id,)).fetchone()
            referee_username = referee_row["username"] if referee_row else ""
        if not referee_username:
            log.warning("Referral bonus delayed: referee %s has no username", referred_id)
            return
        with db_connect() as c:
            ref_user = c.execute("SELECT ref_rewards_received FROM users WHERE chat_id=?", (referrer_id,)).fetchone()
            rewards_received = ref_user["ref_rewards_received"] if ref_user else 0
        if rewards_received >= 10:
            log.info("Referrer %s reached maximum referral reward limit (10).", referrer_id)
            with db_connect() as c:
                c.execute("UPDATE referrals SET bonus_given=1 WHERE referred_id=?", (referred_id,))
            return
        bonus = db_give_referral_bonus(referrer_id, referred_id)
        if bonus:
            with db_connect() as c:
                c.execute("UPDATE users SET ref_rewards_received = ref_rewards_received + 1 WHERE chat_id=?", (referrer_id,))
            log.info("Referral bonus awarded: %d days to %s for referring %s", bonus, referrer_id, referred_id)
            ref_lang = db_get_user_lang(referrer_id)
            if ref_lang == "en":
                ref_text = (
                    f"🎉 *Your friend subscribed to the channel and activated the bot!*\n\n"
                    f"You have been awarded *+{REFERRAL_BONUS_DAYS} free days* of Premium subscription!\n\n"
                    f"/refer — view your invitation stats."
                )
            else:
                ref_text = (
                    f"🎉 *Ваш друг підписався на канал та активував бота!*\n\n"
                    f"Вам нараховано *+{REFERRAL_BONUS_DAYS} безкоштовних днів* Premium-підписки!\n\n"
                    f"/refer — переглянути вашу статистику запрошень."
                )
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=ref_text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.warning("Could not notify referrer %s: %s", referrer_id, e)


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
                 "sl_warning_sent", "last_signal_time", "last_signal_score", "last_check_time")

    def __init__(self) -> None:
        self.entry_price:         float | None = None
        self.running:             bool         = False
        self.waiting_entry_price: float | None = None
        self.sl_warning_sent:     bool         = False
        self.last_signal_time:    float        = 0.0
        self.last_signal_score:   int          = 0
        self.last_check_time:     float        = 0.0

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
_price_history: dict[str, list[tuple[float, float]]] = {p: [] for p in PAIRS}
_last_channel_analysis_slot: tuple[date, int] | None = None
_last_article_hour:          int = -1
_article_index:          int = 0
_last_resolution_check_time: float = 0.0


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
    'TONUSD':  'TONUSDT',
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
    "TONUSD": (0.1,    500),
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

    # TONUSD: CoinGecko when Binance is geo-blocked (common on VPS) — before slow Yahoo cascade
    if pair == "TONUSD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "usd"},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "GoldCryptoTradingBot/1.0",
                },
                timeout=10,
            )
            r.raise_for_status()
            p = float((r.json().get("the-open-network") or {}).get("usd") or 0)
            if p and _valid_price(p, pair):
                log.debug("Price %s = %s (CoinGecko)", pair, p)
                return _save(p)
        except Exception as e:
            log.debug("CoinGecko (%s): %s", pair, e)

    # Yahoo Finance — GC=F for gold, SI=F for silver, direct tickers for crypto
    yahoo_tickers = {
        "XAUUSD": ["GC%3DF"],       # Gold futures
        "XAGUSD": ["SI%3DF"],       # Silver futures
        "BTCUSD": ["BTC-USD"],
        "ETHUSD": ["ETH-USD"],
        "SOLUSD": ["SOL-USD"],
        "XRPUSD": ["XRP-USD"],
        "BNBUSD": ["BNB-USD"],
        # Yahoo "TON-USD" is NOT Toncoin (~$2); Toncoin/The Open Network is TON11419-USD.
        "TONUSD": ["TON11419-USD"],
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
        "TONUSD": "TONUSDT",
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


def _openrouter_rr_bump(pool_tag: str, span: int) -> int:
    """Return current RR offset for this pool and advance the counter."""
    with _openrouter_rr_lock:
        cur = _openrouter_pool_rr.get(pool_tag, 0)
        _openrouter_pool_rr[pool_tag] = cur + 1
        return cur % max(1, span)


def _openrouter_attempt_key_strings(pool_keys: list[str], pool_tag: str) -> list[str]:
    if not pool_keys:
        return []
    n = len(pool_keys)
    start = _openrouter_rr_bump(pool_tag, n)
    ring = pool_keys[start:] + pool_keys[:start]
    now = time.monotonic()
    healthy = [k for k in ring if _OPENROUTER_CREDIT_HOLD_UNTIL.get(k, 0.0) <= now]
    held = [k for k in ring if k not in healthy]
    return healthy + held if healthy else ring


def _openrouter_payment_starved_error(http_status: int, err_msg: str) -> bool:
    """402 from OpenRouter when the account can't pay for requested max_tokens / usage."""
    if http_status != 402:
        return False
    m = (err_msg or "").lower()
    return ("credit" in m) or ("afford" in m) or ("balance" in m) or ("billing" in m)


def _openrouter_hold_key_credit_low(api_key: str) -> None:
    until = time.monotonic() + OPENROUTER_402_CREDIT_HOLD_SEC
    with _openrouter_rr_lock:
        _OPENROUTER_CREDIT_HOLD_UNTIL[api_key] = until
    tail = api_key[-4:] if len(api_key) >= 4 else "****"
    log.warning(
        "OpenRouter key …%s on credit hold ~%ss (HTTP 402). Prefer other keys/credits.",
        tail,
        OPENROUTER_402_CREDIT_HOLD_SEC,
    )


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


def _openrouter_parse_affordable_output_cap(err_msg: str) -> int | None:
    """
    OpenRouter may return 402 with 'can only afford N' — use N to retry with a lower max_tokens.
    """
    if not err_msg:
        return None
    for pat in (
        r"can\s+only\s+afford\s+(\d+)",
        r"only\s+afford\s+(\d+)",
        r"afford\s+(\d+)\s*(?:tokens?|output\b)",
        r"must\s+(?:have|contain)\s+<=\s*(\d+)\s*tokens?",
    ):
        m = re.search(pat, err_msg, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def _openrouter_suggests_fewer_max_tokens(http_status: int, err_msg: str) -> bool:
    """Heuristic credit / budget errors where lowering max_tokens can help."""
    m = (err_msg or "").lower()
    if http_status == 402:
        return True
    if "fewer max_tokens" in m or "more credits" in m or "requires more credits" in m:
        return True
    if "cannot afford" in m or "can't afford" in m:
        return True
    return False


def _openrouter_chat(
    messages: list,
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.35,
    response_format: dict | None = None,
    key_scope: str = "light",
) -> str:
    scope = key_scope if key_scope in ("light", "heavy", "merged") else "light"
    if scope == "light":
        pool = _openrouter_keys_light()
    elif scope == "heavy":
        pool = _openrouter_keys_heavy()
    else:
        pool = _openrouter_keys_merged()

    if not pool:
        raise RuntimeError(
            "OpenRouter key pool is empty for scope %r — set OPENROUTER_API_KEY and/or "
            "OPENROUTER_KEYS_LIGHT / OPENROUTER_KEYS_HEAVY" % scope,
        )

    models_to_try = [m.strip() for m in (model or OPENROUTER_MODEL).split(",") if m.strip()]
    if not models_to_try:
        models_to_try = ["openai/gpt-4o-mini"]

    pool_rr_tag = "merged" if scope == "merged" else scope
    order = _openrouter_attempt_key_strings(pool, pool_rr_tag)
    last_err = ""
    last_sc = 0

    for attempt, api_key in enumerate(order):
        skip_key = False
        for current_model in models_to_try:
            if skip_key:
                break
            payload: dict = {
                "model": current_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if response_format is not None:
                payload["response_format"] = response_format

            cap = max(
                OPENROUTER_OUTPUT_FLOOR,
                min(int(max_tokens), OPENROUTER_HARD_OUTPUT_CAP),
            )
            did_shrink = False
            inner_last_err, inner_last_sc = "", 0
            for _bump in range(12):
                post_payload = dict(payload)
                post_payload["max_tokens"] = cap
                ok, text, sc, err = _openrouter_post_once(api_key, post_payload)
                if ok:
                    if attempt > 0 or did_shrink or current_model != models_to_try[0]:
                        log.info(
                            "OpenRouter: OK using model %s and key …%s (%s pool) after failover",
                            current_model,
                            api_key[-4:] if len(api_key) >= 4 else "****",
                            scope,
                        )
                    return text

                inner_last_err, inner_last_sc = err, sc

                # Check if this is a credit hold error (402)
                if sc == 402 and _openrouter_payment_starved_error(sc, err or ""):
                    _openrouter_hold_key_credit_low(api_key)
                    skip_key = True
                    break

                # If rate limit or transient error, try next model in fallback list
                if sc in (429, 408, 500, 502, 503, 504):
                    log.warning(
                        "OpenRouter: model %s failed with HTTP %s; trying next model in pool",
                        current_model,
                        sc,
                    )
                    break

                afforded = _openrouter_parse_affordable_output_cap(err or "")
                if afforded is not None:
                    nxt = max(OPENROUTER_OUTPUT_FLOOR, afforded - OPENROUTER_AFFORD_MARGIN)
                    if nxt < cap:
                        log.warning(
                            "OpenRouter: lowering max_tokens %s→%s (affordable=%s) key …%s",
                            cap,
                            nxt,
                            afforded,
                            api_key[-4:] if len(api_key) >= 4 else "****",
                        )
                        cap = nxt
                        did_shrink = True
                        continue

                # If API wording changes and we couldn't parse afford, bisect downwards on credit errors.
                if _openrouter_suggests_fewer_max_tokens(sc, err or "") and cap > OPENROUTER_OUTPUT_FLOOR + 48:
                    nxt = max(OPENROUTER_OUTPUT_FLOOR, cap // 2)
                    if nxt < cap:
                        log.warning(
                            "OpenRouter: bisect max_tokens %s→%s (HTTP %s) key …%s",
                            cap,
                            nxt,
                            sc,
                            api_key[-4:] if len(api_key) >= 4 else "****",
                        )
                        cap = nxt
                        did_shrink = True
                        continue

                break

            last_err, last_sc = inner_last_err, inner_last_sc

        # After trying all models for this key, if it wasn't successful:
        if not _openrouter_failover_eligible(last_sc, last_err):
            raise RuntimeError(last_err or f"OpenRouter HTTP {last_sc}")
        if attempt < len(order) - 1:
            log.warning(
                "OpenRouter key …%s failed; trying next key (%s pool)",
                api_key[-4:] if len(api_key) >= 4 else "****",
                scope,
            )

    raise RuntimeError(last_err or f"OpenRouter HTTP {last_sc} (all keys/models exhausted)")


def _openrouter_text(prompt: str, max_tokens: int = 500) -> str:
    """Generate plain text via OpenRouter (light pool)."""
    return _openrouter_chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.45,
        key_scope="light",
    )


def _openrouter_json_analysis(prompt: str) -> str:
    """Ask OpenRouter for a JSON trading signal (light pool)."""
    return _openrouter_chat(
        [{"role": "user", "content": prompt + "\n\nReply with ONLY valid JSON, no markdown code fences."}],
        max_tokens=400,
        temperature=0.25,
        key_scope="light",
    )


def _gemini_text(prompt: str, max_tokens: int = 500) -> str:
    """Generate text via Gemini. Used as Groq fallback after OpenRouter."""
    import google.genai as genai
    import google.genai.types as gtypes

    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            max_output_tokens=max_tokens,
            **_gemini_thinking_kw(),
        ),
    )
    return _gemini_response_visible_text(response, context="gemini_text")


def _gemini_json_analysis(prompt: str) -> str:
    """Ask Gemini for a JSON trading signal (AI_ROUTE_SIGNAL_JSON)."""
    import google.genai as genai
    import google.genai.types as gtypes

    client = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            max_output_tokens=480,
            response_mime_type="application/json",
            **_gemini_thinking_kw(),
        ),
    )
    return _gemini_response_visible_text(response, context="signal_json_gemini")


_AI_ROUTE_BACKEND_TOKENS = frozenset({
    "groq",
    "openrouter_light",
    "openrouter_heavy",
    "openrouter_merged",
    "gemini",
})


def _parse_ai_route_env(env_name: str, default_csv: str) -> tuple[str, ...]:
    """Parse comma-separated backend order from env; invalid tokens are skipped."""
    raw = os.getenv(env_name, "").strip()
    src = raw if raw else default_csv
    out: list[str] = []
    for part in src.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p not in _AI_ROUTE_BACKEND_TOKENS:
            log.warning("%s: unknown backend %r — skipped", env_name, p)
            continue
        out.append(p)
    if not out:
        for part in default_csv.split(","):
            p = part.strip().lower()
            if p in _AI_ROUTE_BACKEND_TOKENS:
                out.append(p)
    return tuple(out)


def _groq_err_is_rate_limit(exc: BaseException) -> bool:
    s = str(exc).lower()
    return ("429" in str(exc)) or ("rate_limit" in s) or ("rate limit" in s)


def _openrouter_scope_for_route_token(token: str) -> str:
    if token == "openrouter_heavy":
        return "heavy"
    if token == "openrouter_merged":
        return "merged"
    return "light"


def _ai_backend_route_ready(token: str) -> bool:
    if token == "groq":
        return bool(GROQ_KEY)
    if token == "gemini":
        return bool(GEMINI_KEY)
    if token == "openrouter_light":
        return bool(_openrouter_keys_light())
    if token == "openrouter_heavy":
        return bool(_openrouter_keys_heavy())
    if token == "openrouter_merged":
        return _openrouter_configured()
    return False


def _ai_route_signal_json() -> tuple[str, ...]:
    return _parse_ai_route_env(
        "AI_ROUTE_SIGNAL_JSON",
        "groq,openrouter_light,gemini",
    )


def _ai_route_article() -> tuple[str, ...]:
    return _parse_ai_route_env(
        "AI_ROUTE_ARTICLE",
        "groq,openrouter_light,gemini",
    )


def _ai_route_deep() -> tuple[str, ...]:
    if os.getenv("AI_ROUTE_DEEP", "").strip():
        return _parse_ai_route_env("AI_ROUTE_DEEP", "gemini,openrouter_heavy")
    pref = (DEEP_ANALYSIS_PROVIDER or "gemini").strip().lower()
    if pref not in ("gemini", "openrouter", "auto"):
        pref = "gemini"
    if pref == "openrouter":
        return ("openrouter_heavy", "gemini")
    # gemini + auto: prefer Gemini for long-context reports, heavy OpenRouter as backup
    return ("gemini", "openrouter_heavy")


def _ai_route_chart_vision() -> tuple[str, ...]:
    if os.getenv("AI_ROUTE_CHART_VISION", "").strip():
        return _parse_ai_route_env("AI_ROUTE_CHART_VISION", "gemini,openrouter_heavy")
    pref = (CHART_VISION_PROVIDER or "gemini").strip().lower()
    if pref not in ("gemini", "openrouter", "auto"):
        pref = "gemini"
    if pref == "openrouter":
        return ("openrouter_heavy", "gemini")
    return ("gemini", "openrouter_heavy")


def _deep_route_step_allowed(step: str) -> bool:
    """Groq skipped for oversized deep prompts."""
    return step != "groq"


def _format_ai_route_errors(errs: list[str], *, route: tuple[str, ...], gemini_hint: bool) -> str:
    """Join multi-step failover errors instead of exposing only the last hop."""
    if not errs:
        return "No AI route steps produced a reply."
    sep = "\n⇢ "
    out = sep.join(str(e).strip() for e in errs if str(e).strip())
    hint = ""
    if gemini_hint and any(step == "gemini" for step in route) and not (GEMINI_KEY or "").strip():
        hint = (
            "\n\nGEMINI_KEY is unset — Gemini steps in AI_ROUTE_* are silently skipped "
            "(no failover after OpenRouter if the route relied on Gemini)."
        )
    return out + hint


def _chart_route_step_allowed(step: str) -> bool:
    """Groq skipped — no multimodal path for chart in this codebase."""
    return step != "groq"


def _invoke_signal_json_llm(analysis_prompt: str, groq_model: str) -> str:
    """Trading-signal JSON: order from AI_ROUTE_SIGNAL_JSON (Groq / OpenRouter / Gemini)."""
    if "/" in groq_model:
        return _openrouter_chat(
            [
                {
                    "role": "user",
                    "content": analysis_prompt
                    + "\n\nReply with ONLY valid JSON, no markdown code fences.",
                }
            ],
            model=groq_model,
            max_tokens=450,
            temperature=0.25,
            key_scope="heavy",
        )

    errs: list[str] = []
    for step in _ai_route_signal_json():
        if not _ai_backend_route_ready(step):
            continue
        try:
            if step == "groq":
                return _groq_client().chat.completions.create(
                    model=groq_model,
                    timeout=GROQ_TIMEOUT,
                    messages=[{"role": "user", "content": analysis_prompt}],
                    temperature=0.3,
                    max_tokens=420,
                ).choices[0].message.content
            if step in ("openrouter_light", "openrouter_heavy", "openrouter_merged"):
                return _openrouter_chat(
                    [
                        {
                            "role": "user",
                            "content": analysis_prompt
                            + "\n\nReply with ONLY valid JSON, no markdown code fences.",
                        }
                    ],
                    max_tokens=400,
                    temperature=0.25,
                    key_scope=_openrouter_scope_for_route_token(step),
                )
            if step == "gemini":
                return _gemini_json_analysis(analysis_prompt)
        except Exception as e:
            if step == "groq" and _groq_err_is_rate_limit(e):
                log.info("Groq rate limit — trying next backend in AI_ROUTE_SIGNAL_JSON")
                continue
            errs.append(f"{step}:{e}")
            log.warning("AI_ROUTE_SIGNAL_JSON step=%s failed: %s", step, str(e)[:220])
            continue
    raise RuntimeError(
        errs[-1]
        if errs
        else "No AI backend available for signals — configure Groq/OpenRouter/Gemini for this route."
    )


def _invoke_article_llm(prompt: str) -> str:
    """Channel article body: order from AI_ROUTE_ARTICLE."""
    errs: list[str] = []
    for step in _ai_route_article():
        if not _ai_backend_route_ready(step):
            continue
        try:
            if step == "groq":
                return (
                    _groq_client()
                    .chat.completions.create(
                        model=GROQ_MODEL_NEWS,
                        timeout=GROQ_TIMEOUT,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.5,
                        max_tokens=500,
                    )
                    .choices[0]
                    .message.content.strip()
                )
            if step in ("openrouter_light", "openrouter_heavy", "openrouter_merged"):
                return _openrouter_chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=500,
                    temperature=0.45,
                    key_scope=_openrouter_scope_for_route_token(step),
                )
            if step == "gemini":
                return _gemini_text(prompt, max_tokens=500).strip()
        except Exception as e:
            if step == "groq" and _groq_err_is_rate_limit(e):
                log.info("Groq rate limit — trying next backend in AI_ROUTE_ARTICLE")
                continue
            errs.append(f"{step}:{e}")
            log.warning("AI_ROUTE_ARTICLE step=%s failed: %s", step, str(e)[:220])
            continue
    raise RuntimeError(
        errs[-1]
        if errs
        else "No AI backend available for articles — configure Groq/OpenRouter/Gemini for this route."
    )


def _get_price_change_pct(pair: str, lookback_seconds: int = 300) -> float:
    """Calculate percentage price change for a pair over a lookback window using _price_history."""
    history = _price_history.get(pair, [])
    if not history:
        return 0.0
    now = time.time()
    target_time = now - lookback_seconds
    
    closest_t = None
    closest_price = None
    min_diff = float('inf')
    for t, p in history:
        diff = abs(t - target_time)
        if diff < min_diff:
            min_diff = diff
            closest_t = t
            closest_price = p
            
    if closest_price is None or min_diff > 60:
        return 0.0
        
    current_price = _prices.get(pair)
    if not current_price:
        return 0.0
        
    return (current_price - closest_price) / closest_price * 100


def _query_perplexity_macro(pair: str) -> str:
    """Query Perplexity Sonar via OpenRouter to get recent news and macro sentiment for Gold (XAUUSD)."""
    macro_prompt = (
        f"Search for recent breaking news, macroeconomic events, Fed announcements, "
        f"and geopolitical updates impacting {pair} (Gold) in the last 1 to 4 hours. "
        f"Provide a concise summary of the key market drivers, current price sentiment (bullish/bearish/neutral), "
        f"and any critical upcoming events. Keep the response within 200 words."
    )
    try:
        res = _openrouter_chat(
            [{"role": "user", "content": macro_prompt}],
            model=ADMIN_MACRO_MODEL,
            max_tokens=300,
            temperature=0.2,
            key_scope="heavy",
        )
        return res.strip()
    except Exception as e:
        log.warning("Perplexity Sonar news query failed: %s", e)
        return "No recent macro news available due to query error."


def _run_hybrid_analysis(pair: str, price: float, tech: dict, trend: str, vol: str) -> dict:
    """Run Perplexity Sonar first for macro news, then Qwen 2.5 72B for technical + macro analysis."""
    macro_context = _query_perplexity_macro(pair)
    log.info("Perplexity Sonar context: %s", macro_context[:200])
    
    cfg = PAIRS[pair]
    sl_hint = cfg["sl_pct"]
    tp_hint = cfg["tp_pct"]
    
    _sent_guess = "neutral"
    if tech.get("ok"):
        bull = sum([tech.get("macd_cross") == "bullish",
                    tech.get("ema_trend")  == "up",
                    tech.get("price_vs_ema20") == "above"])
        _sent_guess = "bullish" if bull >= 2 else ("bearish" if bull == 0 else "neutral")
    _dir_guess = "SELL" if _sent_guess == "bearish" or trend == "down" else "BUY"
    _sl_fb, _tp_fb = _make_sl_tp(price, _dir_guess, sl_hint, tp_hint, pair)
    
    fallback = {
        "sentiment": "neutral", "confidence": 35, "risk_level": "medium",
        "recommendation": "wait",
        "optimal_entry": round(price * (0.998 if _dir_guess == "BUY" else 1.002), _get_precision(pair)),
        "stop_loss":      _sl_fb,
        "take_profit":    _tp_fb,
        "risk_reward":    f"1:{tp_hint / sl_hint:.1f}",
        "entry_reason": "fallback", "main_driver": "fallback",
    }
    
    try:
        analysis_prompt = _elite_signal_analysis_prompt(
            pair, cfg, price, tech, trend, vol, macro_context,
        )
        raw = _invoke_signal_json_llm(analysis_prompt, ADMIN_MODEL)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            parsed["confidence"] = _normalize_confidence(parsed.get("confidence", 35))
            merged = {**fallback, **parsed}
            _sanitize_ai_trade_fields(merged, fallback, price, pair, trend, tech)
            
            econ = {"has_danger": False, "events": []}
            try:
                econ = _check_econ_calendar()
            except Exception:
                pass
            
            score = _calc_score(tech, merged, econ, trend, vol)
            return dict(
                pair=pair,
                price=price,
                trend=trend,
                vol=vol,
                tech=tech,
                ai=merged,
                econ=econ,
                score=score
            )
    except Exception as e:
        log.warning("Hybrid admin analysis failed: %s", e)
        
    econ_fb = {"has_danger": False, "events": []}
    try:
        econ_fb = _check_econ_calendar()
    except Exception:
        pass
    score_fb = _calc_score(tech, fallback, econ_fb, trend, vol)
    
    return dict(
        pair=pair,
        price=price,
        trend=trend,
        vol=vol,
        tech=tech,
        ai=fallback,
        econ=econ_fb,
        score=score_fb
    )


def _get_precision(pair: str) -> int:
    """Get the decimal rounding precision for a given pair."""
    return (
        0 if pair in ("BTCUSD", "ETHUSD", "BNBUSD", "SOLUSD")
        else 4 if pair in ("XRPUSD", "ADAUSD", "TONUSD") else 2
    )


def _make_sl_tp(price: float, direction: str, sl_pct: float, tp_pct: float, pair: str) -> tuple[float, float]:
    """Calculate SL and TP correctly based on trade direction and asset precision."""
    rd = _get_precision(pair)
    if direction == "SELL":
        sl = round(price * (1 + sl_pct / 100), rd)   # SL above entry for SELL
        tp = round(price * (1 - tp_pct / 100), rd)   # TP below entry for SELL
    else:  # BUY
        sl = round(price * (1 - sl_pct / 100), rd)   # SL below entry for BUY
        tp = round(price * (1 + tp_pct / 100), rd)   # TP above entry for BUY
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


def _sanitize_ai_trade_fields(ai: dict, fallback: dict, price: float, pair: str, trend: str, tech: dict | None) -> None:
    """Fill/repair optimal_entry, SL, TP, R/R when model JSON used null (overwrites fallback)."""
    cfg = PAIRS[pair]
    sl_hint, tp_hint = cfg["sl_pct"], cfg["tp_pct"]

    def as_float(x) -> float | None:
        if x is None:
            return None
        if isinstance(x, str) and x.strip().lower() in ("", "null", "none"):
            return None
        try:
            v = float(x)
            if v != v:  # NaN
                return None
            return v
        except (TypeError, ValueError):
            return None

    rd = _get_precision(pair)

    ent = as_float(ai.get("optimal_entry"))
    if ent is None or ent <= 0:
        ent = as_float(fallback.get("optimal_entry")) or float(price)

    ai["optimal_entry"] = round(ent, rd)
    entry = float(ai["optimal_entry"])
    
    # Resolve actual direction using the bot's direction logic
    direction, _ = _direction(ai, trend, tech)
    sl_fb, tp_fb = _make_sl_tp(entry, direction, sl_hint, tp_hint, pair)

    sl = as_float(ai.get("stop_loss"))
    tp = as_float(ai.get("take_profit"))
    if sl is None or sl <= 0:
        sl = sl_fb
    if tp is None or tp <= 0:
        tp = tp_fb

    # Ensure physical consistency based on resolved direction
    if direction == "SELL" and (sl < entry or tp > entry):
        sl, tp = _make_sl_tp(entry, "SELL", sl_hint, tp_hint, pair)
    elif direction == "BUY" and (sl > entry or tp < entry):
        sl, tp = _make_sl_tp(entry, "BUY", sl_hint, tp_hint, pair)

    ai["stop_loss"] = round(float(sl), rd)
    ai["take_profit"] = round(float(tp), rd)

    rr = ai.get("risk_reward")
    if rr is None or (isinstance(rr, str) and not str(rr).strip()) or str(rr).strip().lower() == "null":
        ai["risk_reward"] = fallback.get("risk_reward") or f"1:{tp_hint / sl_hint:.1f}"


def _elite_signal_analysis_prompt(
    pair: str, cfg: dict, price: float, tech: dict, trend: str, vol: str, news_clip: str,
) -> str:
    """Shared instructions for Groq/OpenRouter/Gemini on AI_ROUTE_SIGNAL_JSON (JSON-shaped signal)."""
    tb = "unavailable"
    if tech.get("ok"):
        tb = (
            f"RSI={tech['rsi']}({tech['rsi_zone']}), MACD={tech['macd_cross']}, "
            f"EMA20={tech['ema20']}, EMA50={tech['ema50']}, "
            f"Support={tech['support1']}, Resistance={tech['resist1']}"
        )

    headline = dedent("""
        You are an elite, data-driven Crypto & Forex Trading Signal Agent tuned for Gemini-class analysis.
        Your sole purpose is to analyse the MARKET INPUT (technicals + price snapshot + trimmed news headline) and produce
        a precise, actionable signal for the Telegram bot downstream.

        ### CORE EXECUTION RULES
        1. NO PREAMBLES OR SMALLTALK — conclusions live only inside the JSON.
        2. STRICT TRADING LOGIC — use only quantifiable facts from MARKET INPUT. If data are insufficient or contradictory
           for a high-probability setup, treat it as HOLD: sentiment must be neutral, recommendation avoid, subdued confidence,
           explain briefly in entry_reason (entry_reason + main_driver together ≤ 2 short sentences total).
        3. NO HALLUCINATIONS — do not invent fills, ladders, unseen indicators, tweets, calendar events, or prices not justified
           by MARKET INPUT. If unsure, downgrade confidence and HOLD per rule 2.
        4. SL/TP DIRECTION MUST BE PHYSICALLY CONSISTENT — bullish (BUY stance): SL < entry < TP. Bearish (SELL stance): TP < entry < SL.
        5. BREVITY — use concise institutional phrasing (e.g. RSI bearish divergence, pullback into EMA confluence).

        ### OUTPUT CONTRACT (BOT JSON — HARD REQUIREMENT)
        The bot parses one JSON object only — not plaintext lines like SIGNAL: / ENTRY ZONE:.
        Respond with STRICT JSON matching this schema (no fences, no trailing commentary).
        Gemini may already enforce application/json; still obey exactly.

        Mandatory keys — all REQUIRED; optimal_entry, stop_loss, take_profit MUST be positive numbers (never null):
          sentiment: bullish | bearish | neutral
          confidence: integer 0-100 (NOT a fractional probability — map 0.85 styles to 85)
          risk_level: low | medium | high | extreme
          recommendation: enter_now | wait_for_pullback | wait | avoid
              enter_now = high-conviction active signal
              wait_for_pullback = favourable but needs better timing
              wait = monitoring/sidelines
              avoid = HOLD / no-trade when edge is inadequate (maps to insufficient data stance)
          optimal_entry — one actionable working price anchored to current context (nearest confluence/pullback pivot).
          stop_loss — primary protective stop respecting rule 4.
          take_profit — primary target only (conceptual TP1; omit TP2 entirely from JSON).
            On HOLD setups still populate realistic SL/TP around current price respecting risk_level (never null).
          risk_reward — string like 1:2.5 from SL vs TP distances along the directional vector.
          entry_reason — first nucleus of rationale.
          main_driver — second reinforcing clause (combined cap above).

        Stance mapping (mental model only — encode with JSON keys):
        • BUY-style ⇒ sentiment bullish
        • SELL-style ⇒ sentiment bearish
        • SIGNAL: HOLD / insufficient data ⇒ sentiment neutral + recommendation avoid

        Respond with NOTHING except that JSON object.
        """
    ).strip()
    tail = (
        f"\n\n=== MARKET INPUT ===\n"
        f"Pair: {pair} ({cfg['name']})\n"
        f"Current price (spot reference): {price}\n"
        f"Trend label: {trend}\n"
        f"Volatility label: {vol}\n"
        f"Technicals snapshot: {tb}\n"
        f"News/headlines (truncated): {news_clip}\n"
    )
    return headline + tail


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
    _sl_fb, _tp_fb = _make_sl_tp(price, _dir_guess, sl_hint, tp_hint, pair)

    fallback = {
        "sentiment": "neutral", "confidence": 35, "risk_level": "medium",
        "recommendation": "wait",
        "optimal_entry": round(price * (0.998 if _dir_guess == "BUY" else 1.002), _get_precision(pair)),
        "stop_loss":      _sl_fb,
        "take_profit":    _tp_fb,
        "risk_reward":    f"1:{tp_hint / sl_hint:.1f}",
        "entry_reason": "fallback", "main_driver": "fallback",
    }
    try:
        analysis_prompt = _elite_signal_analysis_prompt(
            pair, cfg, price, tech, trend, vol, news_text[:300],
        )
        raw = _invoke_signal_json_llm(analysis_prompt, groq_model)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            parsed["confidence"] = _normalize_confidence(parsed.get("confidence", 35))
            merged = {**fallback, **parsed}
            _sanitize_ai_trade_fields(merged, fallback, price, pair, trend, tech)
            return merged
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


_analysis_cache: dict[str, dict] = {}
ANALYSIS_CACHE_TTL = 15 * 60  # 15 minutes

def full_analysis(price: float, prev: float | None, pair: str,
                  groq_model: str | None = None, bypass_cache: bool = False) -> dict:
    """
    Run all data fetching in parallel using threads.
    Total time = max(slowest request) instead of sum of all requests.

    ``groq_model`` — Groq chat model id. Default: ``GROQ_MODEL_SIGNALS`` (user-facing).
    Channel / admin broadcast: pass ``GROQ_MODEL_NEWS`` (higher free-tier quota).
    """
    global _analysis_cache
    import concurrent.futures as cf

    now = time.time()
    if not bypass_cache and pair in _analysis_cache:
        cached = _analysis_cache[pair]
        time_diff = now - cached["time"]
        price_diff_pct = abs(price - cached["price"]) / cached["price"] * 100 if cached["price"] else 0

        # Reuse cache if within TTL and price has not moved more than 0.5%
        if time_diff < ANALYSIS_CACHE_TTL and price_diff_pct < 0.5:
            log.info("Using cached full_analysis for %s (age: %d seconds, price diff: %.2f%%)",
                     pair, int(time_diff), price_diff_pct)
            res = cached["result"].copy()
            res["price"] = price
            return res

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

    result = dict(pair=pair, price=price, trend=trend, vol=vol,
                  tech=tech, ai=ai, econ=econ, score=score)

    # Save to global cache
    _analysis_cache[pair] = {
        "time": now,
        "price": price,
        "result": result
    }
    return result


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
    elif pair in ("XRPUSD", "ADAUSD", "TONUSD"):
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
        f"🎯 Entry: *{fmt_price(ai.get('optimal_entry'), pair) if (ai.get('optimal_entry') is not None) else fmt_price(price, pair)}*",
        f"🛑 SL: *{fmt_price(ai['stop_loss'], pair) if ai.get('stop_loss') is not None else '—'}*   "
        f"TP: *{fmt_price(ai['take_profit'], pair) if ai.get('take_profit') is not None else '—'}*",
        f"📐 R/R: *{ai.get('risk_reward') or '—'}*",
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
    sl     = float(ai.get("stop_loss")     or _make_sl_tp(price, dr, cfg["sl_pct"], cfg["tp_pct"], pair)[0])
    tp_pct = cfg["tp_pct"]
    sl_pct = cfg["sl_pct"]
    rd     = _get_precision(pair)
    if dr == "BUY":
        tp1 = round(entry * (1 + tp_pct * 0.5 / 100), rd)
        tp2 = round(entry * (1 + tp_pct / 100), rd)
        tp3 = round(entry * (1 + tp_pct * 1.8 / 100), rd)
    else:
        tp1 = round(entry * (1 - tp_pct * 0.5 / 100), rd)
        tp2 = round(entry * (1 - tp_pct / 100), rd)
        tp3 = round(entry * (1 - tp_pct * 1.8 / 100), rd)

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
        f"🤖 {bot_link_markdown()}",
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
    "Toncoin TON Telegram Open Network blockchain",
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
    return _invoke_article_llm(prompt)


def format_article_post(topic_type: str, body: str) -> str:
    div = "─" * 30
    if topic_type == "edu":
        header = f"📚 *Educational Post*\n{div}"
        footer = f"\n{div}\n💡 _Learn more:_ {bot_link_markdown()}"
    else:
        header = f"📰 *Market News*\n{div}"
        footer = f"\n{div}\n📊 _Signals & analysis:_ {bot_link_markdown()}"
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
    elif "toncoin" in t or "the open network" in t:
        pair = "TONUSD"
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


TRANSLATIONS = {
    "uk": {
        "welcome_ref": "👋 *Вітаємо! Вас запросив друг.*\n\n{prices}\n\nТариф: {emoji} *{plan_label}*  ({days} дн.)\n\nОберіть пару та натисніть кнопку нижче для аналізу 👇",
        "welcome_normal": "🤖 *ШІ Сигнали: Золото та Крипта*\n\n{prices}\n\nТариф: {emoji} *{plan_label}*  ({days} дн.)\n\nОберіть пару та натисніть кнопку нижче для аналізу 👇",
        "btn_pair": "🔀 Пара: {emoji} {name}",
        "btn_analyse": "▶️ Аналіз та вхід",
        "btn_stop": "⏹ Стоп",
        "btn_reset": "🔄 Скинути",
        "btn_status": "📊 Статус угоди",
        "btn_deep": "🧠 Глибокий аналіз",
        "btn_chart": "📸 Графік AI",
        "btn_sub": "💳 Підписка",
        "btn_refer": "🤝 Реферали",
        "btn_lang": "🌐 Мова / Language",
        "btn_back": "↩️ Назад",
        "sub_menu_title": "💳 *Тариф: {emoji} {plan_label}*",
        "sub_trial_left": "🔬 Тріал: залишилось *{days} дн.*",
        "sub_basic_left": "⭐ Базовий: залишилось *{days} дн.*",
        "sub_pro_left": "💎 Pro: залишилось *{days} дн.*",
        "sub_diamond_left": "💠 Diamond: залишилось *{days} дн.*",
        "sub_expired": "❌ *Термін підписки закінчився*",
        "refer_title": "🤝 *Запроси друга — отримай Premium!*\n{line}\n\nЗа кожного друга, який приєднається за вашим посиланням та підпишеться на наш канал:\nВи автоматично отримаєте *+{bonus} безкоштовних днів* Premium-підписки!\n\n*Ваше реферальне посилання:*\n`{link}`\n\n{line}\n📊 *Ваша статистика:*\n👥 Запрошено друзів: *{total}*\n✅ Отримано бонусів: *{bonused}* {bars}\n⏳ В очікуванні підписки: *{pending}*\n🎁 Усього отримано днів: *{earned}*\n\n💡 _Поділіться цим посиланням у соцмережах, надішліть у торгові чати або попросіть друзів скористатися ним!_",
        "refer_callback": "🤝 *Запроси друга — отримай Premium!*\n{line}\n\nПоділіться посиланням → друг приєднується та підписується → ви отримуєте *+{bonus} безкоштовних днів*!\n\n*Ваше посилання:*\n`{link}`\n\n{line}\n👥 Запрошено: *{total}*\n✅ Бонуси: *{bonused}* {bars}\n🎁 Отримано днів: *{earned}*",
        "deep_no_access": "💠 *Глибокий аналіз* доступний на тарифах Тріал (1/день) та Diamond (3/день).\n\nНатисніть /start → 💳 Підписка",
        "deep_trial_limit": "⏳ *Досягнуто ліміту Тріалу* (1/день)\n\nОновіть тариф до 💠 Diamond, щоб отримувати *3 глибокі аналізи на день* для будь-якої пари.\n\nНатисніть /start → 💳 Підписка",
        "deep_daily_limit": "⏳ *Досягнуто денного ліміту* ({limit}/день)\n\nВи використали всі {limit} глибоких аналізів на сьогодні.\nЛіміт скидається опівночі за UTC.",
        "deep_title": "🧠 *Глибокий аналіз {remaining}*\n\nОберіть пару для аналізу:",
        "chart_no_access": "💠 *Аналіз графіка ШІ* доступний лише для користувачів тарифу Diamond.\n\nОновіть тариф до Diamond, щоб розблокувати:\n• 📸 Аналіз графіків за скріншотом\n• Пріоритетні автосигнали\n• Знижений поріг сповіщень\n\nНатисніть /start → 💳 Підписка",
        "chart_usage": "📸 *Як використовувати аналіз графіків:*\n\n1. Відкрийте графік вашого брокера або TradingView\n2. Налаштуйте таймфрейм та індикатори\n3. Зробіть скріншот\n4. Надішліть скріншот цьому боту\n   _(опис фото додавати не обов'язково)_\n\nШІ проаналізує графік і надасть:\n• Напрямок та силу тренду\n• Ключові рівні підтримки та опору\n• Рекомендації щодо входу, SL та TP\n• Загальну рекомендацію щодо угоди",
        "chart_analysing": "🔍 *Аналізуємо ваш графік…*\n_Модель `{model}` — очікування близько 15–45 секунд_",
        "btn_cancel": "❌ Скасувати",
        "btn_enter_now": "✅ Увійти зараз",
        "btn_wait_price": "⏳ Чекати ціни {price}",
        "btn_refresh_analysis": "🔄 Оновити аналіз",
        "main_title": "🤖 *ШІ Сигнали: Золото та Крипта*",
        "choose_pair_hint": "🔀 *Оберіть пару*\n\n🔒 XAG (Срібло) — від тарифу Базовий\n🔒 Крипта _(BTC, ETH, SOL, XRP, BNB, TON, ADA)_ — від тарифу Pro",
        "err_unknown_pair": "❌ Невідома пара.",
        "err_requires_plan": "🔒 *{name}* потребує тарифу Pro або Diamond.",
        "pair_selected": "✅ *{emoji} {name}*\n\nЦіна: *{price}*",
        "lang_menu_title": "🌐 *Оберіть мову інтерфейсу:*",
        "lang_changed": "✅ Мову змінено на Українську!",
        "sub_success_alert": "✅ Дякуємо за підписку! Доступ активовано.",
        "sub_fail_alert": "❌ Ви все ще не підписалися на канал! Будь ласка, підпишіться.",
        "fetching_price": "⏳ *Отримуємо ціну {emoji} {name}…*",
        "err_no_price": "❌ Не вдалося отримати ціну.",
        "analysing": "🔄 *Аналізуємо {emoji} {name}…*\n\n💰 Ціна: *{price}*\n_Отримуємо технічні дані, новини та інсайти ШІ…_",
        "timeout": "⏱ *Перевищено час очікування аналізу.*\n\n_Сервер відповідав занадто довго. Зазвичай це трапляється один раз — спробуйте ще раз._",
        "price_error": "❌ Помилка ціни.",
        "trade_opened": "✅ *{emoji} Угоду відкрито!*\n\n{emoji_dir} Напрямок: *{direction}* {description}\nВхід: *{entry}*\nSL: *{sl}* | TP: *{tp}*",
        "trade_error": "❌ Помилка.",
        "waiting_price": "⏳ Очікування ціни *{price}*",
        "cancelled": "↩️ Скасовано",
        "stopped": "⏹ Зупинено",
        "reset_done": "🔄 *Скинуто*",
        "status_title": "📊 *Статус*",
        "what_to_do": "\n\n*Що ви хочете зробити?*",
    },
    "en": {
        "welcome_ref": "👋 *Welcome! You were invited by a friend.*\n\n{prices}\n\nPlan: {emoji} *{plan_label}*  ({days} days)\n\nChoose a pair and tap ▶️ Analyse & Enter",
        "welcome_normal": "🤖 *Gold & Crypto AI Signals*\n\n{prices}\n\nPlan: {emoji} *{plan_label}*  ({days} days)\n\nChoose a pair and tap ▶️ Analyse & Enter",
        "btn_pair": "🔀 Pair: {emoji} {name}",
        "btn_analyse": "▶️ Analyse & Enter",
        "btn_stop": "⏹ Stop",
        "btn_reset": "🔄 Reset",
        "btn_status": "📊 Trade Status",
        "btn_deep": "🧠 Deep Analysis",
        "btn_chart": "📸 Chart AI",
        "btn_sub": "💳 Subscription",
        "btn_refer": "🤝 Refer & Earn",
        "btn_lang": "🌐 Language / Мова",
        "btn_back": "↩️ Back",
        "sub_menu_title": "💳 *Plan: {emoji} {plan_label}*",
        "sub_trial_left": "🔬 Trial: *{days} days* left",
        "sub_basic_left": "⭐ Basic: *{days} days* left",
        "sub_pro_left": "💎 Pro: *{days} days* left",
        "sub_diamond_left": "💠 Diamond: *{days} days* left",
        "sub_expired": "❌ *Subscription expired*",
        "refer_title": "🤝 *Refer a Friend*\n{line}\n\nFor every friend who joins using your link and subscribes to our channel:\n*+{bonus} free days* added to your plan automatically!\n\n*Your referral link:*\n`{link}`\n\n{line}\n📊 *Your stats:*\n👥 Friends invited: *{total}*\n✅ Bonuses earned: *{bonused}* {bars}\n⏳ Pending: *{pending}*\n🎁 Total days earned: *{earned}*\n\n💡 _Share on social media, send to trading groups,\nor ask your friend to forward it!_",
        "refer_callback": "🤝 *Refer a Friend — Earn Free Days*\n{line}\n\nShare your link → friend joins and subscribes → you get *+{bonus} free days!*\n\n*Your link:*\n`{link}`\n\n{line}\n👥 Invited: *{total}*\n✅ Bonuses: *{bonused}* {bars}\n🎁 Days earned: *{earned}*",
        "deep_no_access": "💠 *Deep Analysis* is available on Trial (1/day) and Diamond (3/day).\n\nTap /start → 💳 Subscription",
        "deep_trial_limit": "⏳ *Trial limit reached* (1/day)\n\nUpgrade to 💠 Diamond to get *3 deep analyses per day* on any pair.\n\nTap /start → 💳 Subscription",
        "deep_daily_limit": "⏳ *Daily limit reached* ({limit}/day)\n\nYou've used all {limit} deep analyses for today.\nResets at midnight UTC.",
        "deep_title": "🧠 *Deep Analysis {remaining}*\n\nChoose a pair to analyse:",
        "chart_no_access": "💠 *Chart AI Analysis* is a Diamond-exclusive feature.\n\nUpgrade to Diamond to unlock:\n• 📸 Screenshot chart analysis\n• Priority auto-signals\n• Lower alert threshold\n\nTap /start → 💳 Subscription",
        "chart_usage": "📸 *How to use Chart Analysis:*\n\n1. Open your broker/TradingView chart\n2. Set your timeframe and indicators\n3. Take a screenshot\n4. Send the screenshot to this bot\n   _(caption is optional)_\n\nThe AI will analyse the chart and give you:\n• Trend direction and strength\n• Key support & resistance levels\n• Entry, SL and TP suggestion\n• Overall trade recommendation",
        "chart_analysing": "🔍 *Analysing your chart…*\n_Model `{model}` — about 15–45 seconds_",
        "btn_cancel": "❌ Cancel",
        "btn_enter_now": "✅ Enter now",
        "btn_wait_price": "⏳ Wait for {price}",
        "btn_refresh_analysis": "🔄 Refresh analysis",
        "main_title": "🤖 *Gold & Crypto AI Signals*",
        "choose_pair_hint": "🔀 *Select pair*\n\n🔒 XAG (Silver) — Basic+\n🔒 Crypto _(BTC, ETH, SOL, XRP, BNB, TON, ADA)_ — Pro+",
        "err_unknown_pair": "❌ Unknown pair.",
        "err_requires_plan": "🔒 *{name}* requires Pro or Diamond plan.",
        "pair_selected": "✅ *{emoji} {name}*\n\nPrice: *{price}*",
        "lang_menu_title": "🌐 *Select your interface language:*",
        "lang_changed": "✅ Language changed to English!",
        "sub_success_alert": "✅ Thank you for subscribing! Access activated.",
        "sub_fail_alert": "❌ You still haven't subscribed to the channel! Please subscribe.",
        "fetching_price": "⏳ *Fetching price {emoji} {name}…*",
        "err_no_price": "❌ Could not get price.",
        "analysing": "🔄 *Analysing {emoji} {name}…*\n\n💰 Price: *{price}*\n_Fetching technicals, news and AI insight…_",
        "timeout": "⏱ *Analysis timed out.*\n\n_The server took too long. This usually happens once — please try again._",
        "price_error": "❌ Price error.",
        "trade_opened": "✅ *{emoji} Trade opened!*\n\n{emoji_dir} Direction: *{direction}* {description}\nEntry: *{entry}*\nSL: *{sl}* | TP: *{tp}*",
        "trade_error": "❌ Error.",
        "waiting_price": "⏳ Waiting for *{price}*",
        "cancelled": "↩️ Cancelled",
        "stopped": "⏹ Stopped",
        "reset_done": "🔄 *Reset*",
        "status_title": "📊 *Status*",
        "what_to_do": "\n\n*What would you like to do?*",
    }
}


def plan_label(p: str, lang: str = "en") -> str:
    if lang == "uk":
        return {"trial": "Тріал", "basic": "Базовий", "pro": "Pro", "diamond": "Diamond",
                "admin": "Адмін", "expired": "Закінчився"}.get(p, p)
    else:
        return {"trial": "Trial", "basic": "Basic", "pro": "Pro", "diamond": "Diamond",
                "admin": "Admin", "expired": "Expired"}.get(p, p)


def _t(key: str, lang: str, **kwargs) -> str:
    """Get translated text for key in the given language."""
    trans = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = trans.get(key, TRANSLATIONS["en"].get(key, key))
    if kwargs:
        return text.format(**kwargs)
    return text


def kb_main(plan: str = "trial", pair: str = DEFAULT_PAIR, deep_left: int | None = None, lang: str = "en") -> InlineKeyboardMarkup:
    cfg = PAIRS[pair]
    rows = [
        [InlineKeyboardButton(_t("btn_pair", lang, emoji=cfg['emoji'], name=cfg['name']), callback_data="choose_pair")],
        [InlineKeyboardButton(_t("btn_analyse", lang), callback_data="start")],
    ]
    if plan in ("basic", "pro", "diamond", "admin", "trial"):
        rows.append([
            InlineKeyboardButton(_t("btn_stop", lang),  callback_data="stop"),
            InlineKeyboardButton(_t("btn_reset", lang), callback_data="reset"),
        ])
        rows.append([InlineKeyboardButton(_t("btn_status", lang), callback_data="status")])
    if plan in ("trial", "diamond", "admin"):
        if plan == "admin":
            deep_label = _t("btn_deep", lang)
        elif plan == "trial":
            left = deep_left if deep_left is not None else 1
            deep_label = f"{_t('btn_deep', lang)} ({left}/1)"
        else:
            left = deep_left if deep_left is not None else DEEP_ANALYSIS_DAILY_LIMIT
            deep_label = f"{_t('btn_deep', lang)} ({left}/{DEEP_ANALYSIS_DAILY_LIMIT})"
        if plan in ("diamond", "admin"):
            rows.append([
                InlineKeyboardButton(deep_label, callback_data="deepanalysis_menu"),
                InlineKeyboardButton(_t("btn_chart", lang), callback_data="chart_ai"),
            ])
        else:
            rows.append([InlineKeyboardButton(deep_label, callback_data="deepanalysis_menu")])
    rows.append([
        InlineKeyboardButton(_t("btn_sub", lang), callback_data="sub_menu"),
        InlineKeyboardButton(_t("btn_refer", lang),  callback_data="refer"),
    ])
    rows.append([
        InlineKeyboardButton(_t("btn_lang", lang), callback_data="lang_menu")
    ])
    return InlineKeyboardMarkup(rows)


def kb_main_for(cid: int, plan: str, pair: str = DEFAULT_PAIR) -> InlineKeyboardMarkup:
    """Build kb_main with correct deep_left counter and language for the given user."""
    lang = db_get_user_lang(cid)
    if plan in ("trial", "diamond"):
        used = db_deepanalysis_count_today(cid)
        limit = 1 if plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
        deep_left = max(0, limit - used)
    else:
        deep_left = None
    return kb_main(plan, pair, deep_left=deep_left, lang=lang)


def kb_pairs(current_pair: str, plan: str, lang: str = "en") -> InlineKeyboardMarkup:
    rows = []
    for pid, cfg in PAIRS.items():
        accessible = plan in cfg["plans"]
        mark  = "✅" if pid == current_pair else ("🔒" if not accessible else "")
        label = f"{mark} {cfg['emoji']} {cfg['name']}" + (" (Pro)" if not accessible else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"pair_{pid}")])
    rows.append([InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_sub(lang: str = "en") -> InlineKeyboardMarkup:
    basic_label = f"⭐ Basic — {PRICE_BASIC}⭐/mo (~$5)" if lang == "en" else f"⭐ Базовий — {PRICE_BASIC}⭐/міс (~$5)"
    basic_3_label = f"⭐ Basic — {PRICE_BASIC_3}⭐/3mo (~$12.5) 🔥" if lang == "en" else f"⭐ Базовий — {PRICE_BASIC_3}⭐/3міс (~$12.5) 🔥"
    pro_label = f"💎 Pro   — {PRICE_PRO}⭐/mo (~$9.99)" if lang == "en" else f"💎 Pro   — {PRICE_PRO}⭐/міс (~$9.99)"
    pro_3_label = f"💎 Pro   — {PRICE_PRO_3}⭐/3mo (~$25) 🔥" if lang == "en" else f"💎 Pro   — {PRICE_PRO_3}⭐/3міс (~$25) 🔥"
    diamond_label = f"💠 Diamond — {PRICE_DIAMOND}⭐/mo (~$19.99)" if lang == "en" else f"💠 Diamond — {PRICE_DIAMOND}⭐/міс (~$19.99)"
    diamond_3_label = f"💠 Diamond — {PRICE_DIAMOND_3}⭐/3mo (~$49.99) 🔥" if lang == "en" else f"💠 Diamond — {PRICE_DIAMOND_3}⭐/3міс (~$49.99) 🔥"

    rows = [
        [InlineKeyboardButton(basic_label,            callback_data="buy_basic_1")],
        [InlineKeyboardButton(basic_3_label,   callback_data="buy_basic_3")],
        [InlineKeyboardButton(pro_label,          callback_data="buy_pro_1")],
        [InlineKeyboardButton(pro_3_label,      callback_data="buy_pro_3")],
        [InlineKeyboardButton(diamond_label,   callback_data="buy_diamond_1")],
        [InlineKeyboardButton(diamond_3_label, callback_data="buy_diamond_3")],
    ]
    # Optional crypto payments (NOWPayments)
    if NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL and NOWPAYMENTS_IPN_SECRET:
        pay_label = "₮ Pay with Crypto (USDT TRC20)" if lang == "en" else "₮ Оплата криптою (USDT TRC20)"
        rows += [
            [InlineKeyboardButton(pay_label, callback_data="crypto_menu")],
        ]
    rows.append([InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_confirm(opt: float, pair: str, lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_t("btn_enter_now", lang),                        callback_data="confirm_now")],
        [InlineKeyboardButton(_t("btn_wait_price", lang, price=fmt_price(opt, pair)), callback_data=f"wait_{opt}")],
        [InlineKeyboardButton(_t("btn_cancel", lang),                           callback_data="cancel")],
        [InlineKeyboardButton(_t("btn_refresh_analysis", lang),                 callback_data="refresh_analysis")],
    ])


def kb_lang(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇺🇦 Українська", callback_data="set_lang_uk"),
            InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en"),
        ],
        [
            InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main"),
        ]
    ])


def sub_info_text(acc: dict, lang: str = "en") -> str:
    plan = acc["plan"];  dl = acc["days_left"]
    lines = [_t("sub_menu_title", lang, emoji=PLAN_EMOJI.get(plan, '?'), plan_label=plan_label(plan, lang)), ""]
    if plan == "trial":
        lines += [_t("sub_trial_left", lang, days=dl), "",
                  "🥇 XAU/USD — ✅", "₿ BTC — 🔒", "Ξ ETH — 🔒", "",
                  f"{_t('btn_deep', lang)} — ✅ _(1/day)_" if lang == "en" else f"{_t('btn_deep', lang)} — ✅ _(1/день)_", ""]
    elif plan == "basic":
        lines += [_t("sub_basic_left", lang, days=dl), "",
                  "🥇 XAU/USD — ✅", "🥈 XAG/USD — ✅",
                  "₿ BTC — 🔒", "Ξ ETH — 🔒", "◎ SOL — 🔒",
                  "✕ XRP — 🔒", "🔶 BNB — 🔒", "🔹 TON — 🔒", "🔵 ADA — 🔒", ""]
    elif plan == "pro":
        lines += [_t("sub_pro_left", lang, days=dl), "",
                  "🥇 XAU — ✅", "🥈 XAG — ✅",
                  "₿ BTC — ✅", "Ξ ETH — ✅", "◎ SOL — ✅",
                  "✕ XRP — ✅", "🔶 BNB — ✅", "🔹 TON — ✅", "🔵 ADA — ✅",
                  "✅ Auto-signals" if lang == "en" else "✅ Автосигнали", ""]
    elif plan == "diamond":
        lines += [_t("sub_diamond_left", lang, days=dl), "",
                  "🥇 XAU — ✅", "🥈 XAG — ✅",
                  "₿ BTC — ✅", "Ξ ETH — ✅", "◎ SOL — ✅",
                  "✕ XRP — ✅", "🔶 BNB — ✅", "🔹 TON — ✅", "🔵 ADA — ✅",
                  "✅ Auto-signals (priority)" if lang == "en" else "✅ Автосигнали (пріоритетні)",
                  "✅ Chart AI screenshot analysis" if lang == "en" else "✅ - Аналіз скріншотів Chart AI",
                  "✅ Priority alerts (lower threshold)" if lang == "en" else "✅ Пріоритетні сповіщення (нижчий поріг)", ""]
    elif plan in ("expired", "none"):
        lines += [_t("sub_expired", lang), ""]

    if lang == "en":
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
            f"💡 _Free trial for new users — 3 days_",
        ]
    else:
        lines += [
            "─" * 30,
            "*⭐ Базовий — $5/міс*",
            "  • Сигнали по золоту XAU/USD",
            "  • ШІ аналіз перед угодою",
            "  • Моніторинг SL / TP",
            f"  1 міс — *{PRICE_BASIC}⭐* (~$5)",
            f"  3 міс — *{PRICE_BASIC_3}⭐* (~$12.5) 🔥 _економія ~17%_",
            "",
            "*💎 Pro — $9.99/міс*",
            "  • Усе з Базового +",
            "  • Валютні пари BTC/USD та ETH/USD",
            "  • Цілодобові автосигнали",
            "  • Пріоритетні сповіщення",
            f"  1 міс — *{PRICE_PRO}⭐* (~$9.99)",
            f"  3 міс — *{PRICE_PRO_3}⭐* (~$25) 🔥 _економія ~17%_",
            "",
            "*💠 Diamond — $19.99/міс*",
            "  • Усе з Pro +",
            "  • 📸 Аналіз графіків за скріншотом (Chart AI)",
            "  • Пріоритетні сигнали (знижений поріг)",
            "  • Скорочений кулдаун автосигналів",
            f"  1 міс — *{PRICE_DIAMOND}⭐* (~$19.99)",
            f"  3 міс — *{PRICE_DIAMOND_3}⭐* (~$49.99) 🔥 _економія ~17%_",
            "",
            f"💡 _Безкоштовний trial для нових — {_trial_duration_ua()}_",
        ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  Global activity + Command handlers
# ═══════════════════════════════════════════════════════════════════

async def global_activity_tracker(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Bump last_active (and profile) on every private chat update; sets user_data flag on first insert."""
    chat = update.effective_chat
    user = update.effective_user
    if user is None or chat is None or chat.type != "private":
        return
    cid = chat.id
    lang = getattr(user, "language_code", "") or ""
    pu = getattr(user, "is_premium", None)
    prem_kw: dict[str, bool] = {}
    if pu is not None:
        prem_kw["is_premium"] = bool(pu)
    inserted = db_upsert_user(
        cid,
        user.username or "",
        user.first_name or "",
        language_code=lang,
        **prem_kw,
    )
    if inserted:
        context.user_data["analytics_just_registered"] = True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid    = update.effective_chat.id
    u      = update.effective_user
    args   = context.args  # e.g. ["ref_123456"] or ["youtube"] or []
    is_new = context.user_data.pop("analytics_just_registered", False)

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

    # ── Register referral (pending channel sub & active check) ────────
    if is_new and referrer_id and referrer_id != cid:
        registered = db_register_referral(referrer_id, cid, source or "ref")
        if registered:
            log.info("Referral registered (pending channel sub): %s → %s (source=%s)", referrer_id, cid, source)

    # ── Enforce channel subscription check ──────────────────
    is_subbed = await check_channel_subscription(context.bot, cid)
    if not is_subbed:
        await check_subscription_and_block(update, context)
        return

    # If subbed, try to award referral bonus immediately (if they joined via ref)
    await try_award_referral_bonus(context.bot, cid)

    acc   = db_access(cid)
    plan  = acc["plan"]

    # Show pairs for this plan; include N/A if feeds fail so new pairs stay visible (e.g. TON behind Binance blocks).
    price_lines = []
    for pid, cfg in PAIRS.items():
        if plan not in cfg["plans"]:
            continue
        px = get_price(pid)
        if px:
            price_lines.append(f"{cfg['emoji']} {cfg['name']}: *{fmt_price(px, pid)}*")
        else:
            price_lines.append(f"{cfg['emoji']} {cfg['name']}: _N/A_")
    prices_text = "\n".join(price_lines) if price_lines else ""

    # Welcome message differs for referred users
    lang = db_get_user_lang(cid)
    if is_new and referrer_id:
        welcome = _t(
            "welcome_ref",
            lang,
            prices=prices_text,
            emoji=PLAN_EMOJI.get(plan, "?"),
            plan_label=plan_label(plan, lang),
            days=acc["days_left"],
        )
    else:
        welcome = _t(
            "welcome_normal",
            lang,
            prices=prices_text,
            emoji=PLAN_EMOJI.get(plan, "?"),
            plan_label=plan_label(plan, lang),
            days=acc["days_left"],
        )

    await update.message.reply_text(
        welcome,
        reply_markup=kb_main_for(cid, plan, DEFAULT_PAIR),
        parse_mode="Markdown",
    )


async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral link and stats."""
    cid = update.effective_chat.id
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

    lang = db_get_user_lang(cid)
    stats = db_referral_stats(cid)
    ref_link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{cid}"

    bars = "🟢" * min(stats["bonused"], 10)
    text = _t(
        "refer_title",
        lang,
        line="─" * 28,
        bonus=REFERRAL_BONUS_DAYS,
        link=ref_link,
        total=stats["total"],
        bonused=stats["bonused"],
        bars=bars,
        pending=stats["pending"],
        earned=stats["days_earned"],
    )
    share_label = "📤 Поділитися посиланням" if lang == "uk" else "📤 Share link"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(share_label, switch_inline_query=ref_link),
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


async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DAU / MAU / langs / Telegram Premium counts (SQLite). Admin only."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    # Use user id, not chat id — private chat IDs match users, groups/saved messages do not.
    if user.id != ADMIN_ID:
        log.info(
            "admin_stats denied: user_id=%s chat_id=%s ADMIN_ID=%s",
            user.id,
            update.effective_chat.id if update.effective_chat else None,
            ADMIN_ID,
        )
        return
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await msg.reply_text(
            "⚠️ Команда /admin_stats працює лише в приватному чаті з ботом.",
        )
        return
    await msg.reply_text(db_analytics_report())


async def cmd_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last N users snapshot (SQLite). Admin only."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    if user.id != ADMIN_ID:
        log.info(
            "admin_users denied user_id=%s chat_id=%s ADMIN_ID=%s",
            user.id,
            update.effective_chat.id if update.effective_chat else None,
            ADMIN_ID,
        )
        return
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await msg.reply_text(
            "⚠️ Команда /admin_users працює лише в приватному чаті з ботом.",
        )
        return
    lim = 20
    if context.args:
        try:
            lim = max(1, min(int(context.args[0]), 100))
        except ValueError:
            await msg.reply_text(
                "❌ Формат: /admin_users [кількість 1–100]\nЗа замовчуванням: 20",
            )
            return
    try:
        chunks = db_admin_users_report(limit=lim)
    except sqlite3.Error as e:
        await msg.reply_text(f"❌ Помилка БД: {e}")
        return
    for i, chunk in enumerate(chunks):
        if i == 0:
            await msg.reply_text(chunk)
        else:
            await msg.reply_text(chunk)


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
        full_analysis, price, _prev_prices.get(pair), pair, GROQ_MODEL_NEWS, True,
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

═══ GENERAL TRADING LOGIC RULES ═══
- All trade setups (SETUP A and SETUP B) MUST be mathematically and physically consistent with their direction:
  - If Direction is BUY: Stop Loss MUST be strictly LESS than the Entry zone price, and all TP targets (TP1, TP2, TP3) MUST be strictly GREATER than the Entry zone price.
  - If Direction is SELL: Stop Loss MUST be strictly GREATER than the Entry zone price, and all TP targets (TP1, TP2, TP3) MUST be strictly LESS than the Entry zone price.
  - The TP levels must be logical: TP1 is closest to Entry, TP2 is further, TP3 is furthest.
  Failure to follow these rules is a critical error.

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


def _deep_analysis_model_label(pair: str | None = None, cid: int | None = None) -> str:
    """First configured backend named in AI_ROUTE_DEEP (excluding Groq here)."""
    if pair == "XAUUSD" and cid == ADMIN_ID:
        return "Qwen 2.5 72B"
    for step in _ai_route_deep():
        if not _deep_route_step_allowed(step) or not _ai_backend_route_ready(step):
            continue
        if step.startswith("openrouter"):
            return OPENROUTER_MODEL
        if step == "gemini":
            return GEMINI_MODEL
    return "—"


def _deep_analysis_config_ok() -> tuple[bool, str]:
    route = _ai_route_deep()
    usable = [
        s for s in route
        if _deep_route_step_allowed(s) and _ai_backend_route_ready(s)
    ]
    if not usable:
        return False, (
            "No AI backend reachable for Deep Analysis. Set `GEMINI_KEY` and/or configure "
            "OpenRouter (`OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_2` or `OPENROUTER_KEYS_*`). "
            "Optional: `AI_ROUTE_DEEP=gemini,openrouter_heavy`."
        )
    return True, ""


def _chart_vision_label() -> str:
    """First configured vision backend in AI_ROUTE_CHART_VISION."""
    for step in _ai_route_chart_vision():
        if not _chart_route_step_allowed(step) or not _ai_backend_route_ready(step):
            continue
        if step.startswith("openrouter"):
            return _openrouter_vision_model()
        if step == "gemini":
            return GEMINI_MODEL
    return "—"


def _chart_vision_config_ok() -> tuple[bool, str]:
    route = _ai_route_chart_vision()
    usable = [
        s for s in route
        if _chart_route_step_allowed(s) and _ai_backend_route_ready(s)
    ]
    if not usable:
        return False, (
            "No vision backend reachable. Set `GEMINI_KEY` and/or heavy OpenRouter keys. "
            "Optional: `AI_ROUTE_CHART_VISION=gemini,openrouter_heavy`."
        )
    return True, ""


def _chart_vision_via_gemini(photo_bytes: bytes, prompt: str) -> str:
    import google.genai as genai
    import google.genai.types as gtypes
    import PIL.Image

    client = genai.Client(api_key=GEMINI_KEY)
    image = PIL.Image.open(io.BytesIO(bytes(photo_bytes)))
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt, image],
        config=gtypes.GenerateContentConfig(
            max_output_tokens=CHART_VISION_MAX_OUTPUT_TOKENS,
            **_gemini_thinking_kw(),
        ),
    )
    return _gemini_response_visible_text(response, context="chart_vision")


def _chart_vision_via_openrouter(
    photo_bytes: bytes,
    prompt: str,
    mime: str,
    *,
    key_scope: str,
) -> str:
    b64 = base64.standard_b64encode(bytes(photo_bytes)).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return _openrouter_chat(
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
        max_tokens=openrouter_chart_effective_output_cap(),
        temperature=0.3,
        key_scope=key_scope,
    )


def _invoke_chart_vision_route(photo_bytes: bytes, prompt: str, mime: str) -> str:
    errs: list[str] = []
    route = _ai_route_chart_vision()
    for step in route:
        if not _chart_route_step_allowed(step) or not _ai_backend_route_ready(step):
            if _chart_route_step_allowed(step):
                log.info(
                    "AI_ROUTE_CHART_VISION: skipping %s (backend not configured or empty pool)",
                    step,
                )
            continue
        scope = _openrouter_scope_for_route_token(step)
        try:
            if step == "gemini":
                return _chart_vision_via_gemini(photo_bytes, prompt)
            if step in ("openrouter_light", "openrouter_heavy", "openrouter_merged"):
                return _chart_vision_via_openrouter(
                    photo_bytes, prompt, mime, key_scope=scope,
                )
        except Exception as e:
            errs.append(f"{step}:{e}")
            log.warning("AI_ROUTE_CHART_VISION step=%s failed: %s", step, str(e)[:260])
            continue
    raise RuntimeError(
        _format_ai_route_errors(errs, route=route, gemini_hint=True),
    )


def _openrouter_deep_analysis(pair: str, price: float, *, key_scope: str = "heavy") -> str:
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
        max_tokens=openrouter_deep_effective_output_cap(),
        temperature=0.35,
        key_scope=key_scope,
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
            max_output_tokens=DEEP_ANALYSIS_MAX_OUTPUT_TOKENS,
            **_gemini_thinking_kw(),
        ),
    )
    return _gemini_response_visible_text(response, context="deep_analysis")


def _deep_analysis_llm_call(pair: str, price: float, cid: int | None = None) -> str:
    if pair == "XAUUSD" and cid == ADMIN_ID:
        try:
            log.info("Running premium Qwen 2.5 72B Deep Analysis for Gold (XAUUSD) for Admin")
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                f_tf    = pool.submit(_get_multi_tf_data, pair)
                f_macro = pool.submit(_get_macro_context, pair)
                f_econ  = pool.submit(_check_econ_calendar)
                try:
                    tf_data = f_tf.result(timeout=25)
                except Exception:
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
                model="qwen/qwen-2.5-72b-instruct",
                max_tokens=openrouter_deep_effective_output_cap(),
                temperature=0.35,
                key_scope="heavy",
            )
        except Exception as e:
            log.warning("Premium XAUUSD Deep Analysis failed: %s. Falling back to default.", e)

    errs: list[str] = []
    route = _ai_route_deep()
    for step in route:
        if not _deep_route_step_allowed(step) or not _ai_backend_route_ready(step):
            if _deep_route_step_allowed(step):
                log.info(
                    "AI_ROUTE_DEEP: skipping %s (backend not configured or empty key pool)",
                    step,
                )
            continue
        try:
            if step == "gemini":
                return _gemini_deep_analysis(pair, price)
            if step in ("openrouter_light", "openrouter_heavy", "openrouter_merged"):
                scope = _openrouter_scope_for_route_token(step)
                return _openrouter_deep_analysis(pair, price, key_scope=scope)
        except Exception as e:
            errs.append(f"{step}:{e}")
            log.warning(
                "AI_ROUTE_DEEP step=%s failed: %s",
                step,
                str(e)[:260],
            )
            continue
    raise RuntimeError(
        _format_ai_route_errors(errs, route=route, gemini_hint=True),
    )


async def cmd_deepanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /deepanalysis          — shows pair selection
      /deepanalysis BTCUSD   — analyse specific pair directly
    Trial: 1/day (XAU only). Diamond: 3/day (all pairs). Admin: unlimited.
    """
    cid = update.effective_chat.id
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

    acc = db_access(cid)
    plan = acc["plan"]

    if plan not in ("trial", "diamond", "admin") and cid != ADMIN_ID:
        await update.message.reply_text(
            "💠 *Глибокий аналіз* доступний на тарифах Тріал (1/день) та Diamond (3/день).\n\n"
            "Натисніть /start → 💳 Підписка",
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
                    "⏳ *Досягнуто ліміту Тріалу* (1/день)\n\n"
                    "Оновіть тариф до 💠 Diamond, щоб отримувати *3 глибокі аналізи на день* для будь-якої пари.\n\n"
                    "Натисніть /start → 💳 Підписка",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"⏳ *Досягнуто денного ліміту* ({limit}/день)\n\n"
                    f"Ви використали всі {limit} глибоких аналізів на сьогодні.\n"
                    f"Ліміт скидається опівночі за UTC.",
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
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="deepanalysis_cancel")])

    used = db_deepanalysis_count_today(cid) if cid != ADMIN_ID else 0
    limit = 1 if plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
    if cid != ADMIN_ID:
        remaining = f"  (залишилось сьогодні: {limit - used})"
    else:
        remaining = ""

    await update.message.reply_text(
        f"🧠 *Глибокий аналіз{remaining}*\n\nОберіть пару для аналізу:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )



def _looks_like_gemini_spend_cap(text: str) -> bool:
    t = (text or "").lower()
    return (
        "resource_exhausted" in t
        or "spending cap" in t
        or "ai.studio/spend" in t
        or "monthly spending cap" in t
    )


def _format_user_visible_llm_failure(exc: BaseException) -> str:
    """Long multi-backend errors were truncated to 200 chars — users only saw the first hop."""
    raw = str(exc).strip() or repr(exc)
    if len(raw) > 3800:
        raw = raw[:1900].rstrip() + "\n…\n" + raw[-1900:].lstrip()
    tip = ""
    if _looks_like_gemini_spend_cap(raw):
        tip = (
            "\n\n—\n💡 Це ліміт витрат Google AI Studio (Gemini), не тариф у Telegram-боті. "
            "Керування cap: https://ai.studio/spend\n"
            "Після помилки Gemini бот намагається OpenRouter, якщо він у ланцюжку AI_ROUTE_DEEP "
            "і є ключі/кредити — нижче може бути друга причина."
        )
    return f"❌ {raw}{tip}"


_deep_analysis_cache: dict[str, dict] = {}
DEEP_ANALYSIS_CACHE_TTL = 15 * 60  # 15 minutes

async def _run_deepanalysis(update_or_query, context, cid: int, acc: dict, pair: str) -> None:
    """Execute deep analysis for the given pair and user."""
    global _deep_analysis_cache
    cfg = PAIRS[pair]

    async def reply(text, **kwargs):
        if hasattr(update_or_query, "message") and update_or_query.message:
            return await update_or_query.message.reply_text(text, **kwargs)
        else:
            return await context.bot.send_message(cid, text, **kwargs)

    price = get_price(pair)
    if not price:
        await reply("❌ Could not get current price.")
        return

    now = time.time()
    use_cache = False
    result = ""

    # Check cache
    if pair in _deep_analysis_cache:
        cached = _deep_analysis_cache[pair]
        time_diff = now - cached["time"]
        price_diff_pct = abs(price - cached["price"]) / cached["price"] * 100 if cached["price"] else 0

        # Reuse cache if within TTL and price did not move significantly
        if time_diff < DEEP_ANALYSIS_CACHE_TTL and price_diff_pct < 0.5:
            log.info("Using cached deep analysis for %s (age: %d seconds, price diff: %.2f%%)",
                     pair, int(time_diff), price_diff_pct)
            result = cached["result"]
            use_cache = True

    if not use_cache:
        # Send loading message
        loading_msg = await reply(
            f"🧠 *Deep Analysis* — {cfg['emoji']} {cfg['name']}\n\n"
            f"Model: `{_deep_analysis_model_label(pair, cid)}`\n"
            f"⏳ Gathering data from 4 timeframes + macro news…\n\n"
            f"_This takes 30-60 seconds — please wait_",
            parse_mode="Markdown",
        )

        try:
            loop   = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _deep_analysis_llm_call, pair, price, cid),
                timeout=120,
            )
            # Store in cache
            _deep_analysis_cache[pair] = {
                "time": now,
                "price": price,
                "result": result
            }
        except asyncio.TimeoutError:
            msg_err = "⏱ Analysis timed out (120s). Please try again."
            if loading_msg:
                try:
                    await loading_msg.edit_text(msg_err, parse_mode="Markdown")
                except Exception:
                    await reply(msg_err, parse_mode="Markdown")
            else:
                await reply(msg_err, parse_mode="Markdown")
            return
        except Exception as e:
            msg_err = _format_user_visible_llm_failure(e)
            if loading_msg:
                try:
                    await loading_msg.edit_text(msg_err)
                except Exception:
                    await reply(msg_err)
            else:
                await reply(msg_err)
            log.error("Deep analysis error: %s", e)
            return

        # Delete loading message to keep chat clean
        if loading_msg:
            try:
                await loading_msg.delete()
            except Exception:
                pass

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

    log.info("Deep analysis: cid=%s %s model=%s price=%s (cached=%s)", cid, pair, _deep_analysis_model_label(), price, use_cache)


# ═══════════════════════════════════════════════════════════════════
#  Vision Chart Analysis — AI_ROUTE_CHART_VISION / CHART_VISION_PROVIDER (Gemini + OpenRouter pools)
# ═══════════════════════════════════════════════════════════════════

async def cmd_chartanalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User sends a chart screenshot → Gemini or OpenRouter (multimodal).
    Usage: send photo with caption /chart or just /chart then send photo
    Available to Diamond plan users only.
    """
    cid = update.effective_chat.id
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

    acc = db_access(cid)
    if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
        await update.message.reply_text(
            "💠 *Аналіз графіка ШІ* доступний лише для користувачів тарифу Diamond.\n\n"
            "Оновіть тариф до Diamond, щоб розблокувати:\n"
            "• 📸 Аналіз графіків за скріншотом\n"
            "• Пріоритетні автосигнали\n"
            "• Знижений поріг сповіщень\n\n"
            "Натисніть /start → 💳 Підписка",
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
            "📸 *Як використовувати аналіз графіків:*\n\n"
            "1. Відкрийте графік вашого брокера або TradingView\n"
            "2. Налаштуйте таймфрейм та індикатори\n"
            "3. Зробіть скріншот\n"
            "4. Надішліть скріншот цьому боту\n"
            "   _(опис фото додавати не обов'язково)_\n\n"
            "ШІ проаналізує графік і надасть:\n"
            "• Напрямок та силу тренду\n"
            "• Ключові рівні підтримки та опору\n"
            "• Рекомендації щодо входу, SL та TP\n"
            "• Загальну рекомендацію щодо угоди",
            parse_mode="Markdown",
        )
        return

    ok, err = _chart_vision_config_ok()
    if not ok:
        await update.message.reply_text(f"❌ {err}", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "🔍 *Аналізуємо ваш графік…*\n"
        f"_Модель `{_chart_vision_label()}` — очікування близько 15–45 секунд_",
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

        mime = getattr(photo, "mime_type", None) or "image/jpeg"
        result = await asyncio.to_thread(
            _invoke_chart_vision_route,
            bytes(photo_bytes),
            prompt,
            mime,
        )

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

        log.info(
            "Chart analysis: cid=%s plan=%s model=%s",
            cid,
            acc["plan"],
            _chart_vision_label(),
        )

    except Exception as e:
        await update.message.reply_text(_format_user_visible_llm_failure(e))
        log.error("Chart analysis error: %s", e)


async def handle_photo(update, context):
    cid = update.effective_chat.id
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

    acc = db_access(cid)
    if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
        return
    if update.message and (update.message.photo or update.message.document):
        await cmd_chartanalysis(update, context)

async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ADMIN_ID:
        return
    await update.message.reply_text("Готую пост для каналу…")
    prices = {p: (fmt_price(v, p) if (v := get_price(p)) else "N/A") for p in PAIRS}

    crypto_nowp = ""
    if NOWPAYMENTS_API_KEY and PUBLIC_BASE_URL.strip() and NOWPAYMENTS_IPN_SECRET.strip():
        crypto_nowp = "• 💳 Також є оплата криптовалютою через NOWPayments.\n"

    trial_line = (
        f"🎁 <b>Безкоштовний trial — {_trial_duration_ua()}</b> для нових користувачів: "
        f"XAU/USD + AI-розбір у боті + <b>до 1 Deep Analysis на день</b> лише по золоту. "
        f"Після trial — обери тариф у боті або доступ зникне.\n\n"
    )

    text = (
        "<b>📊 Gold &amp; Crypto — AI Signals</b>\n"
        "<i>Золото, срібло та 9 крипто-пар: техніка, новини й мульти-провайдерний AI.</i>\n\n"
        "<b>⚡ Що зараз у боті та каналі</b>\n"
        "• Аналітика в канал <b>3× на день</b> (орієнт. 09:15:21 за Києвом, літній час; "
        "розклад через UTC у сервері).\n"
        "• Окремо — <b>освітні та новинні</b> дописи без зайвої реклами.\n"
        "• У боті: <b>передвхідний розбір</b> — RSI, MACD, EMA, SL/TP-ідея, score, "
        "новини й AI з failover: Groq → OpenRouter → Gemini.\n"
        "• <b>Deep Analysis</b> — звіт по кількох ТФ + макро; на trial лише XAU, до 1 на день; "
        "на Diamond до 3 на день і будь-яка пара з підпискою.\n"
        "• 💠 Diamond: розбір <b>скріншоту графіка</b> (vision) + частіші пріоритетні авто-сигнали.\n"
        "• Авто-сигнали для <b>Pro / Diamond</b> під час активного ринку.\n"
        "• 📊 /stats — історія сигналів бота й точність.\n"
        "• 🤝 <b>Отримуй Premium безкоштовно:</b> Запрошуй друзів у бот через /refer і автоматично отримуй безкоштовні дні підписки за кожного друга, який підпишеться на наш канал!\n"
        f"{crypto_nowp}"
        "\n<b>💰 Актуальні ціни (у боті оновлюються частіше)</b>\n"
        f"🥇 XAU/USD  <code>{prices['XAUUSD']}</code>\n"
        f"🥈 XAG/USD  <code>{prices['XAGUSD']}</code>\n"
        f"₿  BTC/USD  <code>{prices['BTCUSD']}</code>\n"
        f"Ξ  ETH/USD  <code>{prices['ETHUSD']}</code>\n"
        f"◎  SOL/USD  <code>{prices['SOLUSD']}</code>\n"
        f"✕  XRP/USD  <code>{prices['XRPUSD']}</code>\n"
        f"🔶 BNB/USD  <code>{prices['BNBUSD']}</code>\n"
        f"🔹 TON/USD  <code>{prices['TONUSD']}</code>\n"
        f"🔵 ADA/USD  <code>{prices['ADAUSD']}</code>\n\n"
        "<b>📦 Тарифи (оплата зорями Telegram у боті)</b>\n\n"
        "⭐ <b>Basic</b> (~$5/міс) — XAU/USD, SL/TP-моніторинг, AI по золоту.\n"
        "Пакет 3 міс — дешевше ≈17%.\n\n"
        "💎 <b>Pro</b> (~$9.99/міс) — Basic + срібло + усі крипто-пари з бота, "
        "<b>авто-сигнали 24/7</b>. 3 міс зі знижкою.\n\n"
        "💠 <b>Diamond</b> (~$19.99/міс) — усе з Pro + Deep по всіх парах (ліміт/день), "
        "Chart Vision і пріоритетні сповіщення. 3 міс bundle вигідніший.\n\n"
        f"{trial_line}"
        f"<b>👇 Старт і підписка</b> — {bot_link_html()}"
    )
    try:
        msg = await context.bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        try:
            await context.bot.pin_chat_message(CHANNEL_ID, msg.message_id,
                                               disable_notification=True)
        except Exception:
            pass
        await update.message.reply_text(
            "✅ Пост опубліковано й закріплено в каналі.\n"
            f"XAU={prices['XAUUSD']} · BTC={prices['BTCUSD']} · ETH={prices['ETHUSD']}"
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
    lang = db_get_user_lang(cid)

    if q.data == "check_subscription_refresh":
        is_subbed = await check_channel_subscription(context.bot, cid)
        if is_subbed:
            try:
                await q.answer(_t("sub_success_alert", lang), show_alert=True)
            except Exception:
                pass
            await try_award_referral_bonus(context.bot, cid)
            try:
                await q.message.delete()
            except Exception:
                pass
            await cmd_start(update, context)
        else:
            try:
                await q.answer(_t("sub_fail_alert", lang), show_alert=True)
            except Exception:
                pass
        return

    # Check channel subscription for all other buttons
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

    # Language Menu Callback
    if q.data == "lang_menu":
        await safe_edit(
            q,
            _t("lang_menu_title", lang),
            markup=kb_lang(lang)
        )
        return

    if q.data.startswith("set_lang_"):
        new_lang = q.data[len("set_lang_"):]
        if new_lang in ("uk", "en"):
            db_set_user_lang(cid, new_lang)
            lang = db_get_user_lang(cid)
            try:
                await q.answer(_t("lang_changed", lang), show_alert=True)
            except Exception:
                pass
            await safe_edit(
                q,
                _t("main_title", lang),
                markup=kb_main_for(cid, plan, u.selected_pair)
            )
        return

    # Deep analysis pair selection
    if q.data == "deepanalysis_cancel":
        await q.message.delete()
        return

    if q.data == "chart_ai":
        if acc["plan"] not in ("diamond", "admin") and cid != ADMIN_ID:
            await safe_edit(
                q,
                _t("chart_no_access", lang),
                markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")]]),
            )
            return
        await safe_edit(q,
            _t("chart_usage", lang),
            markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")]]),
        )
        return

    if q.data == "deepanalysis_menu":
        cur_plan = acc["plan"]
        if cur_plan not in ("trial", "diamond", "admin") and cid != ADMIN_ID:
            await safe_edit(
                q,
                _t("deep_no_access", lang),
                markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")]]),
            )
            return
        if cid != ADMIN_ID:
            used = db_deepanalysis_count_today(cid)
            limit = 1 if cur_plan == "trial" else DEEP_ANALYSIS_DAILY_LIMIT
            if used >= limit:
                if cur_plan == "trial":
                    msg = _t("deep_trial_limit", lang)
                else:
                    msg = _t("deep_daily_limit", lang, limit=limit)
                await safe_edit(
                    q,
                    msg,
                    markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")]]),
                )
                return
            rem_val = limit - used
            remaining = f"  ({rem_val} left today)" if lang == "en" else f"  ({rem_val} залишилось сьогодні)"
        else:
            remaining = ""
        rows = []
        for pid, cfg in PAIRS.items():
            if cur_plan in cfg["plans"] or cid == ADMIN_ID:
                rows.append([InlineKeyboardButton(
                    f"{cfg['emoji']} {cfg['name']}",
                    callback_data=f"deepanalysis_{pid}",
                )])
        rows.append([InlineKeyboardButton(_t("btn_cancel", lang), callback_data="deepanalysis_cancel")])
        await safe_edit(q, _t("deep_title", lang, remaining=remaining), markup=InlineKeyboardMarkup(rows))
        return

    if q.data.startswith("deepanalysis_"):
        pair_key = q.data[len("deepanalysis_"):]
        if pair_key not in PAIRS:
            return
        await q.message.delete()
        await _run_deepanalysis(update, context, cid, acc, pair_key)
        return

    if q.data == "choose_pair":
        await safe_edit(q, _t("choose_pair_hint", lang), markup=kb_pairs(u.selected_pair, plan, lang))
        return

    if q.data.startswith("pair_"):
        new_pair = q.data[5:]
        cfg = PAIRS.get(new_pair)
        if not cfg:
            await safe_edit(q, _t("err_unknown_pair", lang), markup=kb_main_for(cid, plan, u.selected_pair))
            return
        if plan not in cfg["plans"]:
            await safe_edit(q, _t("err_requires_plan", lang, name=cfg['name']), markup=kb_sub(lang))
            return
        u.selected_pair = new_pair
        price = get_price(new_pair)
        price_str = fmt_price(price, new_pair) if price else 'N/A'
        await safe_edit(
            q,
            _t("pair_selected", lang, emoji=cfg['emoji'], name=cfg['name'], price=price_str),
            markup=kb_main_for(cid, plan, new_pair),
        )
        return

    if q.data == "back_main":
        await safe_edit(q, _t("main_title", lang), markup=kb_main_for(cid, plan, u.selected_pair))
        return

    if q.data == "refer":
        stats    = db_referral_stats(cid)
        ref_link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{cid}"
        bars     = "🟢" * min(stats["bonused"], 10)
        text = _t(
            "refer_callback",
            lang,
            line="─" * 28,
            bonus=REFERRAL_BONUS_DAYS,
            link=ref_link,
            total=stats["total"],
            bonused=stats["bonused"],
            bars=bars,
            earned=stats["days_earned"],
        )
        await safe_edit(q, text, markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t("btn_back", lang), callback_data="back_main")],
        ]))
        return

    if q.data == "sub_menu":
        await safe_edit(q, sub_info_text(acc, lang), markup=kb_sub(lang))
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
            [InlineKeyboardButton("₮ Pro — 3 mo ($25)", callback_data="crypto_pay_pro_3")],
            [InlineKeyboardButton("₮ Diamond — 1 mo ($19.99)", callback_data="crypto_pay_diamond_1")],
            [InlineKeyboardButton("₮ Diamond — 3 mo ($49.99)", callback_data="crypto_pay_diamond_3")],
            [InlineKeyboardButton("↩️ Back", callback_data="sub_menu")],
        ])
        crypto_menu_intro = (
            "₮ *Pay with Crypto (USDT TRC20)*\n\n"
            "*UA:* Отримаєш адресу й *точну кількість USDT*. Підписка увімкнеться після підтвердження мережею.\n"
            "*EN:* You get address + *exact USDT amount*; access unlocks after network confirmation.\n\n"
            "⚠️ *Чому немає Basic і коротких планів?*\n"
            "Мінімальні суми *NOWPayments* і мережі означали б для *Basic* криптом значну доплату "
            "(наприклад ~$13 замість $12.5 — цього ми свідомо не пропонуємо). Усе *Basic / Pro на 1 міс* й пакунок "
            "*Basic на 3 міс* доступні через ⭐ у *Subscription*.\n\n"
            "📋 Тут лише:\n• *Pro — 3 місяці*\n• *Diamond — 1 або 3 міс*"
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
                "⚠️ *Цей план оплатити криптою через це меню не можна*\n\n"
                "Посередник *NOWPayments* і мінімуми мережі роблять криптоплатіж по *Basic* "
                "(у тому числі пакету на *3 міс*) або дешевим *Pro на 1 міс* практично неможливим "
                "*без великої надбавки* до цінника — тому ці тарифи криптом ми не показуємо.\n\n"
                "*Що робити:*\n"
                "• *Basic*, *Pro 1 міс* → оплата ⭐ у *Subscription*\n"
                "• Криптом тут лише *Pro на 3 міс* й *Diamond* (1 або 3 міс).\n\n"
                "_Якщо підказка зʼявилась після старої кнопки — онови бота на сервері._"
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
            "₮ *Рахунок USDT (TRC20)*",
            "",
            f"*План:* {plan_label(plan_key)} × *{months}* міс.",
            "",
            "*Сплата — рівно ця сума USDT,* скопіюй символ‑в‑символ:",
            f"`{amt}` *USDT*",
            "",
            "*Адреса:*",
            f"`{addr}`",
            "",
            "⚠️ Не округлюй і не діли суму на кілька платежів — інакше автоактивізація не спрацює.",
            "_Підписка вмикається одразу після підтвердження вашого переказу в блокчейні._",
        ]
        if url:
            lines += ["", f"*Посилання інвойсу:* `{url}`"]
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
        await safe_edit(q, _t("fetching_price", lang, emoji=cfg['emoji'], name=cfg['name']))
        price_val = get_price(pair)
        if not price_val:
            await safe_edit(q, _t("err_no_price", lang), markup=kb_main(plan, pair, lang=lang))
            return
        await safe_edit(q,
            _t("analysing", lang, emoji=cfg['emoji'], name=cfg['name'], price=fmt_price(price_val, pair))
        )
        try:
            loop = asyncio.get_event_loop()
            if cid == ADMIN_ID and pair == "XAUUSD":
                def run_admin_hybrid():
                    ref = _prev_prices.get(pair) or price_val
                    diff = (price_val - ref) / ref * 100
                    trend = "up" if price_val > ref else ("down" if price_val < ref else "flat")
                    vol = "normal" if abs(diff) < 0.5 else ("high" if abs(diff) < 1.0 else "chaos")
                    tech = get_technicals(pair)
                    return _run_hybrid_analysis(pair, price_val, tech, trend, vol)
                a = await asyncio.wait_for(
                    loop.run_in_executor(None, run_admin_hybrid),
                    timeout=60,
                )
            else:
                a = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda pr=price_val, prev=_prev_prices.get(pair), pk=pair: full_analysis(
                            pr, prev, pk, None, True
                        ),
                    ),
                    timeout=45,
                )
        except asyncio.TimeoutError:
            await safe_edit(q,
                _t("timeout", lang),
                markup=kb_main_for(cid, plan, pair),
            )
            return
        u.pending_analysis = a
        opt = float(a["ai"].get("optimal_entry") or price_val)
        await safe_edit(
            q,
            build_analysis_text(a) + _t("what_to_do", lang),
            markup=kb_confirm(opt, pair, lang),
        )
        return

    if q.data == "confirm_now":
        price_val = get_price(pair)
        if not price_val:
            await safe_edit(q, _t("price_error", lang), markup=kb_main_for(cid, plan, pair))
            return
        a  = u.pending_analysis or {}
        ai = a.get("ai", {})
        dr, de = _direction(ai, a.get("trend", "flat"), a.get("tech"))
        ps.entry_price     = price_val
        ps.running         = True
        ps.sl_warning_sent = False
        ps.persist(cid, pair)
        sl_fb, tp_fb = _make_sl_tp(price_val, dr, cfg["sl_pct"], cfg["tp_pct"], pair)
        sl = ai.get("stop_loss") or sl_fb
        tp = ai.get("take_profit") or tp_fb
        # Save to signals table for backtesting
        db_save_signal(
            pair, dr, price_val, float(sl), float(tp),
            a.get("score", 0), ai.get("sentiment", "neutral"), source="user",
        )
        await safe_edit(
            q,
            _t("trade_opened", lang, emoji=cfg['emoji'], emoji_dir=('🟢' if dr == 'BUY' else '🔴'), direction=dr, description=de, entry=fmt_price(price_val, pair), sl=sl, tp=tp),
        )
        return

    if q.data.startswith("wait_"):
        try:
            opt = float(q.data[5:])
        except ValueError:
            await safe_edit(q, _t("trade_error", lang), markup=kb_main_for(cid, plan, pair))
            return
        ps.waiting_entry_price = opt
        ps.persist(cid, pair)
        await safe_edit(q, _t("waiting_price", lang, price=fmt_price(opt, pair)),
                        markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "cancel":
        u.pending_analysis = None
        await safe_edit(q, _t("cancelled", lang), markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "stop":
        ps.running = False
        ps.persist(cid, pair)
        await safe_edit(q, _t("stopped", lang), markup=kb_main_for(cid, plan, pair))
        return

    if q.data == "reset":
        ps.reset(cid, pair)
        u.pending_analysis = None
        await safe_edit(q, _t("reset_done", lang), markup=kb_main_for(cid, plan, pair))
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
        msg = "\n\n".join(lines) if lines else ("ℹ️ Немає активних угод" if lang == "uk" else "ℹ️ No active trades")
        await safe_edit(q, f"{_t('status_title', lang)}\n\n{msg}", markup=kb_main_for(cid, plan, u.selected_pair))
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
    lang    = db_get_user_lang(cid)
    log.info("💰 PAYMENT: @%s | %s x%d mo | %d⭐ | until %s",
             uname, plan_label(pk, lang), months, stars, new_exp.strftime("%d.%m.%Y"))

    if lang == "uk":
        msg = (
            f"✅ *Оплату отримано!*\n\n"
            f"{PLAN_EMOJI.get(pk, '⭐')} *{plan_label(pk, lang)}*\n"
            f"Активний до: *{new_exp.strftime('%d.%m.%Y')}*\n"
            f"⭐ {stars} Зірок\n\nНатисніть /start"
        )
    else:
        msg = (
            f"✅ *Payment received!*\n\n"
            f"{PLAN_EMOJI.get(pk, '⭐')} *{plan_label(pk, lang)}*\n"
            f"Active until: *{new_exp.strftime('%d.%m.%Y')}*\n"
            f"⭐ {stars} Stars\n\nTap /start"
        )

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💰 *New payment!*\n@{uname} | {plan_label(pk, 'en')} x{months} mo | {stars}⭐\n"
            f"Until: {new_exp.strftime('%d.%m.%Y')}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Admin notification failed: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  Background monitor
# ═══════════════════════════════════════════════════════════════════

async def monitor(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_channel_analysis_slot, _last_article_hour, _article_index, _last_resolution_check_time

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
                if pair not in _price_history:
                    _price_history[pair] = []
                _price_history[pair].append((time.time(), p))
                _price_history[pair] = [
                    (t, val) for t, val in _price_history[pair]
                    if time.time() - t <= 1200
                ]

        now_utc = datetime.now(UTC)
        h = now_utc.hour

        # 2) Channel market analysis: 1 pair per calendar hour (09–18 in CHANNEL_ANALYSIS_TZ) → 10 LLM/day total.
        dt_ch = channel_analysis_local_datetime()
        d_ch, h_ch = dt_ch.date(), dt_ch.hour
        h0 = CHANNEL_ANALYSIS_LOCAL_START_HOUR
        h1 = h0 + CHANNEL_ANALYSIS_HOURLY_SLOTS
        if h0 <= h_ch < h1:
            slot_key = (d_ch, h_ch)
            if slot_key != _last_channel_analysis_slot:
                slot_idx = h_ch - h0
                pairs_ord = channel_scheduled_analysis_pairs()
                pair = pairs_ord[slot_idx % len(pairs_ord)]
                price = _prices.get(pair)
                if not price:
                    log.warning(
                        "Channel hourly: awaiting price for %s (%s slot %02d)",
                        pair,
                        CHANNEL_ANALYSIS_TZ_NAME,
                        h_ch,
                    )
                else:
                    pt = post_type_for_channel_local_hour(h_ch)
                    try:
                        a = await asyncio.to_thread(
                            full_analysis,
                            price,
                            _prev_prices.get(pair),
                            pair,
                            GROQ_MODEL_NEWS,
                            True,
                        )
                        text = groq_channel_post(a, pt)
                        cfg = PAIRS[pair]
                        sent = await safe_send_photo(context.bot, CHANNEL_ID, cfg["image"], text)
                        if not sent:
                            await safe_send(context.bot, CHANNEL_ID, text)
                        db_save_post(pair, pt, a["score"], a["ai"].get("sentiment", "?"), price, 0)
                        ai = a["ai"]
                        dr, _ = _direction(ai, a["trend"], a.get("tech"))
                        sl_fb, tp_fb = _make_sl_tp(price, dr, cfg["sl_pct"], cfg["tp_pct"], pair)
                        sl = ai.get("stop_loss") or sl_fb
                        tp = ai.get("take_profit") or tp_fb
                        db_save_signal(
                            pair,
                            dr,
                            price,
                            float(sl),
                            float(tp),
                            a["score"],
                            ai.get("sentiment", "neutral"),
                            source="ai",
                        )
                        log.info(
                            "Channel hourly tz=%s date=%s %02d:00 slot=%d %s score=%s",
                            CHANNEL_ANALYSIS_TZ_NAME,
                            d_ch,
                            h_ch,
                            slot_idx,
                            pair,
                            a["score"],
                        )
                    except Exception as e:
                        log.error("Channel post error (%s): %s", pair, e)
                    else:
                        _last_channel_analysis_slot = slot_key

        # 3) Educational / news articles — Groq only in ARTICLE_HOURS_UTC, GROQ_MODEL_NEWS via groq_article().
        if channel_articles_enabled() and h in ARTICLE_HOURS_UTC and h != _last_article_hour:
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
                    check_interval = 5 * 60 if plan in ("diamond", "admin") else 15 * 60

                    if time.time() - ps.last_signal_time > cooldown:
                        if cid == ADMIN_ID and pair == "XAUUSD":
                            change_5m = _get_price_change_pct("XAUUSD", 300)
                            if abs(change_5m) >= XAU_VOLATILITY_THRESHOLD:
                                ps.last_check_time = time.time()
                                log.info(
                                    "XAUUSD volatility spike detected: %+.2f%%. Running hybrid admin analysis...",
                                    change_5m,
                                )
                                try:
                                    tech_snap = get_technicals(pair)
                                    a = await asyncio.to_thread(
                                        _run_hybrid_analysis,
                                        pair,
                                        price,
                                        tech_snap,
                                        "up" if change_5m > 0 else "down",
                                        "high" if abs(change_5m) >= XAU_VOLATILITY_THRESHOLD * 2 else "normal",
                                    )
                                    if a["score"] >= score_min:
                                        priority_tag = " ⚡ *Reactive Volatility*"
                                        text = (build_analysis_text(a)
                                                + f"\n\n📡 *Reactive Signal!*{priority_tag} 5m Move: *{change_5m:+.2f}%* | Score: *{a['score']}/100*")
                                        await safe_send(context.bot, cid, text,
                                                        reply_markup=kb_main_for(cid, plan, pair))
                                        ps.last_signal_time  = time.time()
                                        ps.last_signal_score = a["score"]
                                        ps.persist(cid, pair)
                                except Exception as ex:
                                    log.error("Failed running hybrid admin analysis: %s", ex)
                        else:
                            if time.time() - ps.last_check_time > check_interval:
                                prev = _prev_prices.get(pair)
                                if prev:
                                    ps.last_check_time = time.time()
                                    a = await asyncio.to_thread(
                                        full_analysis, price, prev, pair,
                                    )
                                    if a["score"] >= score_min:
                                        priority_tag = " 💠 *Priority*" if plan == "diamond" else ""
                                        text = (build_analysis_text(a)
                                                + f"\n\n📡 *Auto-signal!*{priority_tag} Score: *{a['score']}/100*")
                                        await safe_send(context.bot, cid, text,
                                                        reply_markup=kb_main_for(cid, plan, pair))
                                        ps.last_signal_time  = time.time()
                                        ps.last_signal_score = a["score"]
                                        ps.persist(cid, pair)

        # 5) Auto-resolve signals and broadcast resolutions (every 5 minutes)
        now_time = time.time()
        if now_time - _last_resolution_check_time >= 300:
            _last_resolution_check_time = now_time
            try:
                loop = asyncio.get_running_loop()
                resolved_sigs = await loop.run_in_executor(None, _resolve_open_signals)
                if resolved_sigs:
                    log.info("Monitor resolved %d signal(s). Broadcasting...", len(resolved_sigs))
                    await broadcast_signal_resolution(context.bot, resolved_sigs)
            except Exception as e:
                log.error("Monitor signal resolution failed: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  Signal tracking helpers
# ═══════════════════════════════════════════════════════════════════

def forward_signal_to_scalper(pair: str, direction: str, entry: float):
    """Forward signal to scalping bot's FastAPI webhook asynchronously."""
    import urllib.request
    import json
    import threading

    # Map pair to Bybit Swap ticker
    ticker = pair.upper()
    if ticker == "XAUUSD":
        ticker = "XAUUSDT"
    elif ticker == "BTCUSD":
        ticker = "BTCUSDT"
    elif "/" not in ticker and not ticker.endswith("USDT"):
        ticker = f"{ticker}USDT"

    # Map direction to LONG/SHORT
    dir_map = {
        "BUY": "LONG",
        "SELL": "SHORT",
        "LONG": "LONG",
        "SHORT": "SHORT"
    }
    mapped_dir = dir_map.get(direction.upper(), "LONG")

    # Construct JSON payload
    payload = {
        "ticker": ticker,
        "direction": mapped_dir,
        "entry_price": float(entry)
    }

    # Scalper webhook URL
    webhook_url = os.getenv("SCALPER_WEBHOOK_URL", "http://127.0.0.1:8000/webhook")

    def send():
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res = response.read().decode("utf-8")
                log.info("Forwarded signal to scalper: %s -> Response: %s", payload, res)
        except Exception as e:
            log.warning("Failed to forward signal to scalper: %s", e)

    # Run in a background thread to prevent blocking the main Telegram bot event loop
    threading.Thread(target=send, daemon=True).start()


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
        sig_id = cur.lastrowid
        # Forward the signal to the scalping bot
        try:
            forward_signal_to_scalper(pair, direction, entry)
        except Exception as e:
            log.error("Error launching forward_signal_to_scalper: %s", e)
        return sig_id


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

def _resolve_open_signals() -> list:
    """
    For every unresolved signal check historical data to see if
    TP or SL was hit. Returns list of newly resolved signals details.
    """
    try:
        import yfinance as yf
    except ImportError:
        return []

    from datetime import timezone, timedelta
    open_sigs = db_get_open_signals(days=30)
    resolved  = []

    for sig in open_sigs:
        pair      = sig["pair"]
        direction = sig["direction"]
        entry     = sig["entry_price"]
        sl        = sig["sl_price"]
        tp        = sig["tp_price"]
        posted_at = sig["posted_at"]

        # Skip checking if the signal is very new (less than 5 minutes old)
        posted_dt = datetime.strptime(posted_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        if (now_dt - posted_dt).total_seconds() < 300:
            continue

        try:
            # Download 1-minute bars from signal time to now
            ticker = PAIRS[pair]["yahoo"]
            # Subtract 1 minute to ensure start is strictly before end in case of slight clock drift
            start  = posted_dt - timedelta(minutes=1)
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
                resolved.append({
                    "id": sig["id"],
                    "pair": pair,
                    "direction": direction,
                    "entry_price": entry,
                    "sl_price": sl,
                    "tp_price": tp,
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "message_id": sig.get("message_id", 0)
                })

        except Exception as e:
            log.debug("Backtest resolve error (signal %s): %s", sig["id"], e)

    return resolved


async def broadcast_signal_resolution(bot, resolved_sigs: list) -> None:
    """Broadcast resolved signals to the channel and active users with referral profit cards."""
    for sig in resolved_sigs:
        pair      = sig["pair"]
        direction = sig["direction"]
        entry     = sig["entry_price"]
        sl        = sig["sl_price"]
        tp        = sig["tp_price"]
        outcome   = sig["outcome"]
        pnl       = sig["pnl_pct"]
        
        if outcome not in ("TP", "SL"):
            continue

        cfg = PAIRS.get(pair)
        if not cfg:
            continue

        emoji = cfg.get("emoji", "📈")
        name = cfg.get("name", pair)

        status_emoji = "🎯" if outcome == "TP" else "❌"
        outcome_label = "Take-Profit" if outcome == "TP" else "Stop-Loss"
        exit_price = tp if outcome == "TP" else sl
        pnl_sign = "+" if pnl > 0 else ""
        pnl_str = f"{pnl_sign}{pnl}%"

        # 1. Post to Channel
        channel_text = (
            f"{status_emoji} *{outcome_label} виконано по {name} ({pair})!*\n\n"
            f"💰 Результат: *{pnl_str}* прибутку\n"
            f"📈 Вхід: *{fmt_price(entry, pair)}* | Вихід: *{fmt_price(exit_price, pair)}*\n"
            f"🤖 Сигнал розрахований нашою моделлю ШІ.\n\n"
            f"🔗 Приєднуйтесь та отримуйте сигнали: @{BOT_USERNAME.lstrip('@')}"
        )
        try:
            await safe_send(bot, CHANNEL_ID, channel_text, parse_mode="Markdown")
        except Exception as e:
            log.error("Failed to post resolution to channel: %s", e)

        # 2. DM to allowed/active users
        try:
            with db_connect() as c:
                users = c.execute("SELECT chat_id FROM users").fetchall()
        except Exception as e:
            log.error("Failed to fetch users from DB: %s", e)
            users = []

        for u_row in users:
            user_id = u_row["chat_id"]
            if str(user_id) == str(CHANNEL_ID):
                continue

            try:
                acc = db_access(user_id)
                if not acc["allowed"]:
                    continue
            except Exception:
                continue

            ref_link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{user_id}"
            user_text = (
                f"{status_emoji} *{outcome_label} виконано по {name} ({pair})!*\n\n"
                f"💰 Результат: *{pnl_str}* прибутку\n"
                f"📈 Вхід: *{fmt_price(entry, pair)}* | Вихід: *{fmt_price(exit_price, pair)}*\n"
                f"🤖 Сигнал розрахований нашою моделлю ШІ.\n\n"
                f"🔗 Отримати ці сигнали безкоштовно на 3 дні:\n"
                f"`{ref_link}`"
            )

            share_text = (
                f"🎯 {outcome_label} виконано по {name}!\n\n"
                f"Результат: {pnl_str} прибутку\n"
                f"Вхід: {fmt_price(entry, pair)} | Вихід: {fmt_price(exit_price, pair)}\n\n"
                f"Отримати ці сигнали безкоштовно на 3 дні:\n"
                f"{ref_link}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Поділитися прибутком 🚀", switch_inline_query=share_text)]
            ])

            try:
                await safe_send(bot, user_id, user_text, parse_mode="Markdown", reply_markup=kb)
            except Exception as e:
                log.debug("Failed to send resolution card to user %s: %s", user_id, e)


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
    if not await check_channel_subscription(context.bot, cid):
        await check_subscription_and_block(update, context)
        return

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
    resolved_sigs = await loop.run_in_executor(None, _resolve_open_signals)
    resolved_count = len(resolved_sigs)

    text = _backtest_stats_text(pair, days)
    if resolved_count:
        text += f"\n\n_🔄 {resolved_count} signal(s) just resolved_"

    # Add pair selector buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 All pairs",  callback_data="stats_ALL_30"),
         InlineKeyboardButton("🥇 XAU/USD",    callback_data="stats_XAUUSD_30")],
        [InlineKeyboardButton("₿ BTC/USD",     callback_data="stats_BTCUSD_30"),
         InlineKeyboardButton("Ξ ETH/USD",     callback_data="stats_ETHUSD_30")],
        [InlineKeyboardButton("🔹 TON/USD",     callback_data="stats_TONUSD_30"),
         InlineKeyboardButton("◎ SOL/USD",     callback_data="stats_SOLUSD_30")],
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
        f"▶️ Details → {bot_link_markdown()}"
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
        f"Use any ticker key from `/start` picker (e.g. `XAUUSD`, `BTCUSD`, `TONUSD`).\n\n"
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
    resolved_sigs = await loop.run_in_executor(None, _resolve_open_signals)
    resolved_count = len(resolved_sigs)

    text = _backtest_stats_text(pair_filter, days)
    if resolved_count:
        text += f"\n\n_🔄 {resolved_count} signal(s) just resolved_"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 All pairs",  callback_data="stats_ALL_30"),
         InlineKeyboardButton("🥇 XAU/USD",    callback_data="stats_XAUUSD_30")],
        [InlineKeyboardButton("₿ BTC/USD",     callback_data="stats_BTCUSD_30"),
         InlineKeyboardButton("Ξ ETH/USD",     callback_data="stats_ETHUSD_30")],
        [InlineKeyboardButton("🔹 TON/USD",     callback_data="stats_TONUSD_30"),
         InlineKeyboardButton("◎ SOL/USD",     callback_data="stats_SOLUSD_30")],
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
    ch_pairs = channel_scheduled_analysis_pairs()
    log.info(
        "Channel analysis: hourly in tz=%s, %02d:00–%02d:59 → %d Groq posts/day "
        "(tz: default UTC unless CHANNEL_ANALYSIS_TZ set; pairs: CHANNEL_ANALYSIS_PAIRS or PAIRS order)",
        CHANNEL_ANALYSIS_TZ_NAME,
        CHANNEL_ANALYSIS_LOCAL_START_HOUR,
        CHANNEL_ANALYSIS_LOCAL_START_HOUR + CHANNEL_ANALYSIS_HOURLY_SLOTS - 1,
        CHANNEL_ANALYSIS_HOURLY_SLOTS,
    )
    log.info("Channel pair rotation base order: %s", ",".join(ch_pairs))
    if not channel_articles_enabled():
        log.info("Channel articles off (enable with CHANNEL_ARTICLES_ENABLED=1)")
    log.info(
        "Groq: channel/articles=%s | user signals=%s | monitor interval=%ss",
        GROQ_MODEL_NEWS,
        GROQ_MODEL_SIGNALS,
        MONITOR_INTERVAL_SEC,
    )
    if _openrouter_configured():
        log.info(
            "OpenRouter: %d merged key(s) — light/heavy pools + failover (429 / credit hold)",
            len(_openrouter_keys_merged()),
        )
    else:
        log.info("OpenRouter: not configured (optional; used after Groq 429 with Gemini fallback)")

    app = ApplicationBuilder().token(TOKEN).build()
    _APP_REF = app  # store reference for TV webhook handler

    app.add_handler(TypeHandler(Update, global_activity_tracker), group=-1)

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("refer",        cmd_refer))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("tvinfo",       cmd_tvinfo))
    app.add_handler(CommandHandler("deepanalysis", cmd_deepanalysis))
    app.add_handler(CommandHandler("chart",        cmd_chartanalysis))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CommandHandler("admin_stats", cmd_admin_stats))
    app.add_handler(CommandHandler("admin_users", cmd_admin_users))
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
