import os
import json
import asyncio
import logging

logger = logging.getLogger("scalping_bot.state")

class StateManager:
    def __init__(self, filepath: str = "state.json"):
        self.filepath = filepath
        self.lock = asyncio.Lock()
        self.state = {
            "daily_trade_count": 0,
            "active_trade_ticker": None,
            "last_reset_date": ""
        }
        self._load()

    def _load(self):
        """Loads state from JSON file if it exists, otherwise creates it with default values."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self.state = json.load(f)
                logger.info("State loaded successfully from file.")
            except Exception as e:
                logger.error(f"Failed to load state file, using default state: {e}")
        else:
            self._save_sync()

    def _save_sync(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to write state file: {e}")

    async def save(self):
        """Saves current state asynchronously."""
        async with self.lock:
            # Run blocking I/O in a thread executor or just run it synchronously since it is tiny
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save_sync)

    # --- Daily Trade Counter ---
    def get_daily_trade_count(self) -> int:
        return self.state.get("daily_trade_count", 0)

    async def increment_daily_trade_count(self):
        async with self.lock:
            self.state["daily_trade_count"] = self.state.get("daily_trade_count", 0) + 1
        await self.save()

    def get_last_reset_date(self) -> str:
        return self.state.get("last_reset_date", "")

    async def reset_daily_counter(self, today_str: str):
        async with self.lock:
            self.state["daily_trade_count"] = 0
            self.state["last_reset_date"] = today_str
        await self.save()

    # --- Active Trade tracking ---
    def has_active_trade(self) -> bool:
        return self.state.get("active_trade_ticker") is not None

    def get_active_trade_ticker(self) -> str | None:
        return self.state.get("active_trade_ticker")

    async def set_active_trade(self, ticker: str | None):
        async with self.lock:
            self.state["active_trade_ticker"] = ticker
        await self.save()
