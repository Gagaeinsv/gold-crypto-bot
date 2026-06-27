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

def fetch_current_price(asset: str) -> float:
    asset = asset.upper().strip()
    
    # Binance API check for crypto
    crypto_suffixes = ("USDT", "BUSD", "USDC", "BTC", "ETH")
    if any(asset.endswith(suffix) for suffix in crypto_suffixes):
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={asset}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                return float(data["price"])
        except Exception:
            pass
            
    # Yahoo Finance fallback
    try:
        yf_asset = asset
        if asset.endswith("USDT"):
            yf_asset = asset.replace("USDT", "-USD")
        
        ticker = yf.Ticker(yf_asset)
        info = ticker.fast_info
        if info and "lastPrice" in info:
            return float(info["lastPrice"])
            
        hist = ticker.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"Error fetching price via Yahoo Finance for {asset}: {e}")
        
    raise ValueError(f"Failed to fetch price for {asset}")

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
                        
                        # Calculate PnL percentage
                        if direction == "BUY":
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
                            tp_triggered = current_price >= entry_price * (1 + Config.DEFAULT_TP_PCT)
                            sl_triggered = current_price <= entry_price * (1 - Config.DEFAULT_SL_PCT)
                        else:  # SELL
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100.0
                            tp_triggered = current_price <= entry_price * (1 - Config.DEFAULT_TP_PCT)
                            sl_triggered = current_price >= entry_price * (1 + Config.DEFAULT_SL_PCT)
                            
                        if tp_triggered or sl_triggered:
                            trigger_type = "Take Profit" if tp_triggered else "Stop Loss"
                            logger.info(f"Trade #{trade_id} hit {trigger_type} target at price {current_price}. Closing. PnL: {pnl_pct:.2f}%")
                            db_engine.close_trade(trade_id, current_price, pnl_pct)
                            
                            if pnl_pct >= 1.0:
                                logger.info(f"Trade #{trade_id} closed with profit. Triggering VideoEngine...")
                                try:
                                    from services.video_engine import VideoEngine
                                    # Fetch overall win rate
                                    metrics = db_engine.get_metrics()
                                    trade_data = {
                                        "id": trade_id,
                                        "asset": asset,
                                        "direction": direction,
                                        "pnl_percentage": pnl_pct
                                    }
                                    # Run synchronous video generation in a separate thread to avoid blocking event loop
                                    asyncio.create_task(asyncio.to_thread(VideoEngine.generate_shorts, trade_data, metrics))
                                except Exception as video_ex:
                                    logger.error(f"Failed to start VideoEngine task for trade #{trade_id}: {video_ex}")
                                    
                    except Exception as ex:
                        logger.error(f"Error checking trade #{trade_id} ({asset}): {ex}")
            else:
                logger.debug("No open trades to track.")
        except Exception as e:
            logger.error(f"Error in price tracker loop iteration: {e}")
            
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
