import asyncio
import logging
import os
import urllib.request
import json
import yfinance as yf
from config import Config
from services.storage_engine import StorageEngine
from services.telegram_parser import TelegramParserService

# Ensure storage directory exists
os.makedirs("storage", exist_ok=True)

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("storage/bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("main_orchestrator")

# Correct Yahoo Finance tickers for commodities and metals
YAHOO_TICKER_MAP = {
    "XAUUSD": "GC=F",   # Gold Futures
    "XAGUSD": "SI=F",   # Silver Futures
    "XPTUSD": "PL=F",   # Platinum Futures
}

# Binance crypto mapping (USD → USDT suffix for Binance API)
BINANCE_TICKER_MAP = {
    "BTCUSD":  "BTCUSDT",
    "ETHUSD":  "ETHUSDT",
    "BNBUSD":  "BNBUSDT",
    "XRPUSD":  "XRPUSDT",
    "ADAUSD":  "ADAUSDT",
    "SOLUSD":  "SOLUSDT",
    "TONUSD":  "TONUSDT",
    "DOTUSD":  "DOTUSDT",
    "LINKUSD": "LINKUSDT",
    "AVAXUSD": "AVAXUSDT",
}

def fetch_current_price(asset: str) -> float:
    asset = asset.upper().strip().replace("/", "").replace("$", "")

    # 1. Try Binance API for crypto (fastest and most reliable)
    binance_symbol = BINANCE_TICKER_MAP.get(asset)
    if not binance_symbol and any(asset.endswith(s) for s in ("USDT", "USDC", "BUSD", "BTC", "ETH")):
        binance_symbol = asset
    
    if binance_symbol:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                price = float(data["price"])
                if price > 0:
                    return price
        except Exception:
            pass

    # 2. Yahoo Finance fallback with correct ticker mapping
    yf_asset = YAHOO_TICKER_MAP.get(asset)
    if not yf_asset:
        if asset.endswith("USDT"):
            yf_asset = asset[:-4] + "-USD"
        elif asset.endswith("USD"):
            yf_asset = asset[:-3] + "-USD"
        else:
            yf_asset = asset

    try:
        logger.info(f"Fetching Yahoo Finance price for: {yf_asset} (original: {asset})")
        ticker = yf.Ticker(yf_asset)
        price = getattr(ticker.fast_info, "last_price", None)
        if price and float(price) > 0:
            return float(price)
        hist = ticker.history(period="1d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            if price > 0:
                return price
    except Exception as e:
        logger.error(f"Yahoo Finance error for {asset} (as {yf_asset}): {e}")

    raise ValueError(f"Failed to fetch valid price for {asset}")

async def price_tracker_loop(db_engine: StorageEngine):
    logger.info("Price tracker loop started.")
    while True:
        try:
            open_trades = db_engine.get_open_trades()
            if open_trades:
                logger.info(f"Checking prices for {len(open_trades)} open trades...")
                for trade in open_trades:
                    trade_id = trade["id"]
                    asset = trade["asset"]
                    direction = trade["direction"]
                    entry_price = trade["entry_price"]
                    
                    try:
                        current_price = fetch_current_price(asset)
                        logger.info(f"Trade #{trade_id} ({asset} {direction}): Entry={entry_price}, Current={current_price}")
                        
                        # ── SAFETY GUARD: reject obviously wrong prices ──────────
                        if current_price <= 0:
                            logger.warning(f"Trade #{trade_id}: Got zero/negative price ({current_price}) for {asset}. Skipping.")
                            continue
                        
                        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
                        if direction == "SELL":
                            pnl_pct = -pnl_pct

                        if abs(pnl_pct) > 40:
                            logger.warning(f"Trade #{trade_id}: Anomalous PnL {pnl_pct:.1f}% for {asset}. Likely wrong price. Skipping.")
                            continue
                        # ─────────────────────────────────────────────────────────
                        
                        # Calculate TP/SL triggers
                        if direction == "BUY":
                            tp_triggered = current_price >= entry_price * (1 + Config.DEFAULT_TP_PCT)
                            sl_triggered = current_price <= entry_price * (1 - Config.DEFAULT_SL_PCT)
                        else:  # SELL
                            tp_triggered = current_price <= entry_price * (1 - Config.DEFAULT_TP_PCT)
                            sl_triggered = current_price >= entry_price * (1 + Config.DEFAULT_SL_PCT)
                            
                        if tp_triggered or sl_triggered:
                            trigger_type = "Take Profit" if tp_triggered else "Stop Loss"
                            logger.info(f"Trade #{trade_id} hit {trigger_type} at {current_price}. PnL: {pnl_pct:.2f}%")
                            db_engine.close_trade(trade_id, current_price, pnl_pct)
                            
                            if pnl_pct >= 1.0:
                                logger.info(f"Trade #{trade_id} closed with profit. Triggering VideoEngine...")
                                try:
                                    from services.video_engine import VideoEngine
                                    free_metrics = db_engine.get_metrics(source="ai")
                                    vip_metrics = db_engine.get_metrics(source="user")
                                    trade_data = {
                                        "id": trade_id,
                                        "asset": asset,
                                        "direction": direction,
                                        "entry_price": entry_price,
                                        "exit_price": current_price,
                                        "pnl_percentage": pnl_pct
                                    }
                                    asyncio.create_task(asyncio.to_thread(VideoEngine.generate_shorts, trade_data, free_metrics, vip_metrics))
                                except Exception as video_ex:
                                    logger.error(f"Failed to start VideoEngine for trade #{trade_id}: {video_ex}")
                                    
                    except Exception as ex:
                        logger.error(f"Error checking trade #{trade_id} ({asset}): {ex}")
            else:
                logger.debug("No open trades to track.")
        except Exception as e:
            logger.error(f"Error in price tracker loop: {e}")
            
        await asyncio.sleep(60)

async def main():
    logger.info("Initializing Storage Engine...")
    db_engine = StorageEngine()
    
    logger.info("Initializing Telegram Ingestion Engine...")
    parser_service = TelegramParserService(db_engine)
    
    # Start price tracker task
    tracker_task = asyncio.create_task(price_tracker_loop(db_engine))
    
    # Start Telegram parser task if credentials are set
    if Config.TELEGRAM_API_ID and Config.TELEGRAM_API_HASH:
        try:
            # Gather tasks so they run concurrently
            await asyncio.gather(
                parser_service.start(),
                tracker_task
            )
        except Exception as e:
            logger.critical(f"Telegram parser failed: {e}. Running price tracker only.")
            await tracker_task
    else:
        logger.warning("Telegram credentials missing in .env. Running price tracker loop only.")
        await tracker_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested. Exiting...")
