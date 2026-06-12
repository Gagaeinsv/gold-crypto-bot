import uvicorn
import asyncio
import logging
from config import Config
from state import StateManager
from risk import RiskGuard
from exchange import ExchangeManager
from receiver import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scalping_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("scalping_bot.main")

# Initialize shared components
state_manager = StateManager("state.json")
risk_guard = RiskGuard(state_manager, Config)
exchange_manager = ExchangeManager(state_manager, Config)

async def orchestrate_trade(ticker: str, direction: str, size_usd: float | None, entry_price_override: float | None):
    """
    Main trade orchestrator triggered by signal webhook.
    Runs pre-trade risk checks, executes the entry market order,
    calculates SL/TP levels, and submits protective orders to the exchange.
    """
    # Calculate trade size: use dynamic percentage if configured, otherwise fallback to fixed size
    if Config.POSITION_SIZE_PCT > 0.0:
        logger.info(f"Calculating trade size dynamically as {Config.POSITION_SIZE_PCT}% of balance...")
        trade_size = await exchange_manager.calculate_size_from_pct(Config.POSITION_SIZE_PCT)
    else:
        trade_size = size_usd if size_usd is not None else Config.DEFAULT_SIZE_USD
    
    # 1. Pre-trade Risk Check
    allowed, reason = await risk_guard.can_trade(ticker, direction)
    if not allowed:
        logger.warning(f"Trade rejected by RiskGuard for {ticker} ({direction}): {reason}")
        return

    # 2. Lock state and increment trade count
    # Lock the position tracking state immediately to prevent race conditions from consecutive webhooks
    await state_manager.set_active_trade(ticker)
    await state_manager.increment_daily_trade_count()
    
    logger.info(f"Risk checks passed. Commencing execution for {ticker} | Size: ${trade_size}")
    
    try:
        # 3. Open Market Position
        fill_price, filled_amount = await exchange_manager.open_position(ticker, direction, trade_size)
        
        # 4. Calculate protective price levels based on exact fill price
        sl_price, tp_price = risk_guard.calculate_sl_tp(direction, fill_price, ticker)
        
        # 5. Place Stop-Loss and Take-Profit orders on exchange
        sl_id, tp_id = await exchange_manager.place_protective_orders(
            ticker=ticker,
            direction=direction,
            entry_price=fill_price,
            size=filled_amount,
            sl_price=sl_price,
            tp_price=tp_price
        )
        
        # 6. Spawn Background WebSocket listener task to monitor order fills
        asyncio.create_task(
            exchange_manager.watch_and_manage_position(
                ticker=ticker,
                sl_id=sl_id,
                tp_id=tp_id
            )
        )
        
    except Exception as e:
        logger.critical(f"FATAL execution error during trade setup for {ticker}: {e}")
        # Reset state active position if execution failed so we aren't permanently locked out
        await state_manager.set_active_trade(None)

# Create the FastAPI app injecting the orchestrator function
app = create_app(orchestrate_trade)

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutdown event triggered. Closing WebSocket connections...")
    await exchange_manager.close()

if __name__ == "__main__":
    logger.info(f"Starting Scalping Bot Webhook Server on {Config.HOST}:{Config.PORT}")
    uvicorn.run(app, host=Config.HOST, port=Config.PORT)
