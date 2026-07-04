"""
Quick connection test for all AI services and APIs.
Run: /root/venv/bin/python3 test_connections.py
"""
import os, sys, json, time
from dotenv import load_dotenv
load_dotenv()

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = []

def check(name, fn):
    try:
        msg = fn()
        print(f"{PASS} {name}: {msg}")
        results.append((name, True, msg))
    except Exception as e:
        print(f"{FAIL} {name}: {e}")
        results.append((name, False, str(e)))

# ── 1. Groq API + model ──────────────────────────────────────────────────────
def test_groq():
    import httpx
    key = os.getenv("GROQ_KEY", "")
    model = os.getenv("GROQ_MODEL", os.getenv("GROQ_MODEL_SIGNALS", "qwen/qwen3-32b"))
    if not key:
        raise Exception("GROQ_KEY not set in .env")
    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": "Reply with just: OK"}], "max_tokens": 5},
        timeout=10
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return f"model={model} → reply='{reply}'"

check("Groq API (qwen/qwen3-32b)", test_groq)

# ── 2. Groq old model check ───────────────────────────────────────────────────
def test_groq_old_model():
    import httpx
    key = os.getenv("GROQ_KEY", "")
    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": "OK"}], "max_tokens": 5},
        timeout=10
    )
    if resp.status_code == 404 or "decommissioned" in resp.text.lower() or "deprecated" in resp.text.lower():
        raise Exception(f"Model IS deprecated! Status={resp.status_code}")
    resp.raise_for_status()
    return f"Still works (status={resp.status_code}) — but will stop Aug 16!"

check("Groq old model (llama-3.3-70b) — should still work but deprecated", test_groq_old_model)

# ── 3. Gemini API ────────────────────────────────────────────────────────────
def test_gemini():
    import httpx
    key = os.getenv("GEMINI_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not key:
        raise Exception("GEMINI_KEY not set in .env")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    resp = httpx.post(url, json={"contents": [{"parts": [{"text": "Reply with just: OK"}]}]}, timeout=15)
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    return f"model={model} → reply='{text[:30]}'"

check("Gemini API", test_gemini)

# ── 4. Replicate API ─────────────────────────────────────────────────────────
def test_replicate():
    import httpx
    token = os.getenv("REPLICATE_API_TOKEN", "")
    if not token:
        raise Exception("REPLICATE_API_TOKEN not set in .env")
    resp = httpx.get(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    return f"FLUX Schnell model found: {data.get('name', 'OK')}"

check("Replicate API (FLUX Schnell)", test_replicate)

# ── 5. Binance prices ────────────────────────────────────────────────────────
def test_binance():
    import urllib.request
    pairs = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "TONUSDT", "XRPUSDT"]
    prices = {}
    for sym in pairs:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            prices[sym] = float(json.loads(r.read())["price"])
    return " | ".join(f"{k}={v:.4f}" for k, v in prices.items())

check("Binance API (crypto prices)", test_binance)

# ── 6. Yahoo Finance (metals) ─────────────────────────────────────────────────
def test_yfinance():
    import yfinance as yf
    results_yf = {}
    for ticker, name in [("GC=F", "XAUUSD"), ("SI=F", "XAGUSD")]:
        t = yf.Ticker(ticker)
        price = getattr(t.fast_info, "last_price", None)
        if not price or float(price) <= 0:
            hist = t.history(period="1d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if not price or float(price) <= 0:
            raise Exception(f"{name} returned zero/null price!")
        results_yf[name] = round(float(price), 2)
    return " | ".join(f"{k}={v}" for k, v in results_yf.items())

check("Yahoo Finance (Gold GC=F, Silver SI=F)", test_yfinance)

# ── 7. Database ───────────────────────────────────────────────────────────────
def test_db():
    import sqlite3
    db = os.getenv("DATABASE_PATH", "users.db")
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM signals")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM signals WHERE outcome IS NULL")
    open_t = c.fetchone()[0]
    conn.close()
    return f"total signals={total}, open trades={open_t}"

check("SQLite Database", test_db)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "─" * 60)
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"Result: {passed}/{total} checks passed")
if passed == total:
    print("🎉 All systems operational!")
else:
    print("⚠️  Some checks failed — see above for details.")
