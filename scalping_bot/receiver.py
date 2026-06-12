import time
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

logger = logging.getLogger("scalping_bot.receiver")

# Define payload schema
class SignalPayload(BaseModel):
    ticker: str = Field(..., example="SOL/USDT")
    direction: str = Field(..., example="LONG") # LONG or SHORT
    entry_price: float | None = Field(default=None, description="Optional limit/trigger price")
    size_usd: float | None = Field(default=None, description="Optional trade size in USD")

def create_app(orchestrate_trade_func):
    app = FastAPI(title="Scalping Bot Signal Ingestion Receiver")
    
    @app.post("/webhook")
    async def receive_signal(payload: SignalPayload, background_tasks: BackgroundTasks):
        start_time = time.perf_counter()
        
        # Performance check
        direction = payload.direction.upper()
        if direction not in ("LONG", "SHORT"):
            raise HTTPException(status_code=400, detail="Direction must be LONG or SHORT")
            
        logger.info(f"Signal received: Ticker={payload.ticker} | Dir={direction} | Size=${payload.size_usd}")
        
        # Add trade execution pipeline as a background task to keep webhook response < 10ms
        background_tasks.add_task(
            orchestrate_trade_func,
            payload.ticker,
            direction,
            payload.size_usd,
            payload.entry_price
        )
        
        processing_time_ms = (time.perf_counter() - start_time) * 1000
        logger.info(f"Signal ingested and queued in {processing_time_ms:.3f}ms")
        
        return {
            "status": "queued",
            "ticker": payload.ticker,
            "direction": direction,
            "ingest_time_ms": round(processing_time_ms, 3)
        }
        
    return app
