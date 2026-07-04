import logging
import re
import urllib.request
import json
from telethon import TelegramClient, events
from config import Config
from services.storage_engine import StorageEngine

logger = logging.getLogger("telegram_parser")

class TelegramParserService:
    def __init__(self, db_engine: StorageEngine):
        self.db = db_engine
        self.client = None

    def parse_message_text(self, text: str) -> dict:
        if not text:
            return None
            
        cleaned = text.strip()
        asset = None
        direction = None
        entry_price = None
        
        # Regex for asset: e.g. BTCUSDT, ETHUSDT, EURUSD, AAPL, BTC/USDT (uppercase letters, optional slash, numbers)
        asset_matches = re.finditer(r'\b([A-Z]{2,6}/?[A-Z0-9]{2,6})\b', cleaned)
        for m in asset_matches:
            val = m.group(1).replace("/", "").upper()
            if val not in ("BUY", "LONG", "SELL", "SHORT", "ALERT", "SIGNAL", "ZONE", "ENTRY", "PRICE", "TARGET", "STOP", "LOSS", "TAKE", "PROFIT"):
                asset = val
                break
            
        # Direction: BUY, LONG, SELL, SHORT
        dir_match = re.search(r'\b(BUY|LONG|SELL|SHORT)\b', cleaned, re.IGNORECASE)
        if dir_match:
            val = dir_match.group(1).upper()
            direction = "BUY" if val in ("BUY", "LONG") else "SELL"
            
        # Entry price: look for numbers preceded by symbols or keywords like @, entry, buy at, zone
        price_match = re.search(r'(?:@|entry|price|at|zone|buy|sell)\b.*?\b(\d+(?:\.\d+)?)', cleaned, re.IGNORECASE)
        if price_match:
            try:
                entry_price = float(price_match.group(1))
            except ValueError:
                pass
        
        # 2. Fallback to Gemini API if regex parsing is incomplete and API key exists
        if not (asset and direction and entry_price) and Config.GEMINI_API_KEY:
            logger.info("Regex parser incomplete. Invoking Gemini API fallback parser...")
            gemini_result = self.parse_with_gemini(cleaned, Config.GEMINI_API_KEY)
            if gemini_result:
                asset = gemini_result.get("asset") or asset
                direction = gemini_result.get("direction") or direction
                entry_price = gemini_result.get("entry_price") or entry_price
                
        if asset and direction and entry_price:
            return {
                "asset": asset.upper(),
                "direction": direction.upper(),
                "entry_price": float(entry_price),
                "status": "OPEN"
            }
        return None

    def parse_with_gemini(self, text: str, api_key: str) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        prompt = f"""
        Analyze the following trading signal message and extract:
        1. Asset (e.g. BTCUSDT, ETHUSDT, EURUSD, AAPL)
        2. Direction (BUY or SELL)
        3. Entry Price (float)

        Respond ONLY with a JSON object in this format (no markdown blocks, no prefix/suffix):
        {{
          "asset": "BTCUSDT",
          "direction": "BUY",
          "entry_price": 95100.5
        }}

        If the message does not contain a clear trading signal, respond with:
        {{
          "asset": null,
          "direction": null,
          "entry_price": null
        }}

        Message:
        {text}
        """
        
        data = {
            "contents": [{
                "parts": [{"text": prompt}]
            }]
        }
        
        headers = {'Content-Type': 'application/json'}
        req = urllib.request.Request(
            url, 
            data=json.dumps(data).encode('utf-8'), 
            headers=headers, 
            method='POST'
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                resp_data = json.loads(response.read().decode('utf-8'))
                text_response = resp_data['candidates'][0]['content']['parts'][0]['text'].strip()
                
                # Clean potential markdown wrapping
                if text_response.startswith("```"):
                    text_response = re.sub(r'^```(?:json)?\s*|\s*```$', '', text_response, flags=re.MULTILINE).strip()
                
                result = json.loads(text_response)
                return result
        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}")
            return None

    def _evaluate_signal_quality(self, signal: dict, raw_text: str) -> bool:
        """Use Groq AI to evaluate if a free signal is worth taking. Returns True to accept."""
        try:
            import httpx
            if not Config.GROQ_API_KEY:
                return True  # No key = accept all

            asset = signal.get("asset", "")
            direction = signal.get("direction", "")
            entry = signal.get("entry_price", 0)

            prompt = f"""You are a professional crypto trading signal analyst.
Evaluate this trading signal and decide if it is worth taking.

Signal:
- Asset: {asset}
- Direction: {direction}
- Entry Price: {entry}
- Raw message: \"{raw_text[:200]}\"

Rules for ACCEPT:
- Signal has clear asset, direction, and entry price
- The direction makes sense (not buying obvious resistance, not selling obvious support)
- Signal is not suspiciously vague or generic

Rules for REJECT:
- Missing key information
- Contradictory signals (e.g. entry price far from current market)
- Looks like spam or news, not a real signal

Respond with ONLY one word: ACCEPT or REJECT"""

            with httpx.Client(timeout=8.0) as client:
                resp = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {Config.GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 5
                    }
                )
                if resp.status_code == 200:
                    verdict = resp.json()["choices"][0]["message"]["content"].strip().upper()
                    accepted = "ACCEPT" in verdict
                    logger.info(f"AI Signal Filter verdict for {asset} {direction}: {verdict}")
                    return accepted
        except Exception as e:
            logger.warning(f"AI signal filter failed ({e}), accepting signal by default.")
        return True  # On error, accept

    async def start(self):
        if not Config.TELEGRAM_API_ID or not Config.TELEGRAM_API_HASH:
            logger.warning("TELEGRAM_API_ID or TELEGRAM_API_HASH not set. Parser cannot start.")
            return

        self.client = TelegramClient(Config.TELEGRAM_SESSION_NAME, int(Config.TELEGRAM_API_ID), Config.TELEGRAM_API_HASH)
        
        @self.client.on(events.NewMessage(chats=Config.TELEGRAM_CHANNELS))
        async def handler(event):
            logger.info(f"New message received from channel: {event.message.text}")
            
            # Determine source label
            source_label = "ai"  # Default to free
            chat = await event.get_chat()
            chat_username = f"@{chat.username}" if getattr(chat, 'username', None) else ""
            chat_title = getattr(chat, 'title', '') or ''
            chat_id = event.chat_id
            
            logger.info(f"Signal received from — ID: {chat_id} | Username: '{chat_username}' | Title: '{chat_title}'")
            
            # If it comes from the premium bot, tag it as 'user' (Premium in dashboard)
            if "gold_xau_gagarinsv_bot" in chat_username.lower() or "gold_xau_gagarinsv_bot" in chat_title.lower():
                source_label = "user"
                logger.info(f"Tagged as VIP (user) source.")
            else:
                logger.info(f"Tagged as FREE (ai) source.")
                
            signal = self.parse_message_text(event.message.text)
            if signal:
                logger.info(f"Successfully parsed signal: {signal}")

                # AI quality filter for FREE signals only
                if source_label == "ai":
                    accepted = self._evaluate_signal_quality(signal, event.message.text or "")
                    if not accepted:
                        logger.info(f"AI Filter REJECTED signal: {signal['asset']} {signal['direction']} — skipping.")
                        return

                trade_id = self.db.save_trade(
                    asset=signal["asset"],
                    direction=signal["direction"],
                    entry_price=signal["entry_price"],
                    source=source_label
                )
                logger.info(f"Logged new trade ID {trade_id} with source={source_label}.")
            else:
                logger.info("Message could not be parsed into a valid trading signal.")

        logger.info(f"Starting Telethon client. Listening to channels: {Config.TELEGRAM_CHANNELS}")
        await self.client.start()
        logger.info("Telethon client connected successfully. Waiting for signals...")
        await self.client.run_until_disconnected()
