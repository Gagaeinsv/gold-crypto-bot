import logging
from datetime import datetime, UTC
from config import Config
from state import StateManager

logger = logging.getLogger("scalping_bot.risk")

class RiskGuard:
    def __init__(self, state_manager: StateManager, config: Config = Config):
        self.state = state_manager
        self.config = config

    async def can_trade(self, ticker: str, direction: str) -> tuple[bool, str]:
        """
        Runs the strict pre-trade validation rules.
        Returns:
            (bool, reason): True if allowed, False with reason if blocked.
        """
        # 1. Reset daily counter if a new calendar day in UTC has started
        await self._check_daily_reset()

        # 1.5. Check Allowed Tickers
        if self.config.ALLOWED_TICKERS and ticker.upper() not in self.config.ALLOWED_TICKERS:
            reason = f"Blocked: Ticker {ticker} is not in ALLOWED_TICKERS list."
            logger.warning(reason)
            return False, reason

        # 2. Hard Rule A: Max Daily Trades
        current_daily_count = self.state.get_daily_trade_count()
        if current_daily_count >= self.config.MAX_DAILY_TRADES:
            reason = f"Blocked: Daily trade limit reached ({current_daily_count}/{self.config.MAX_DAILY_TRADES})"
            logger.warning(reason)
            return False, reason

        # 3. Hard Rule B: Max Concurrent Trades
        # Check if there is an active trade marked in the local state
        if self.state.has_active_trade():
            active_ticker = self.state.get_active_trade_ticker()
            reason = f"Blocked: Active concurrent trade open on {active_ticker}"
            logger.warning(reason)
            return False, reason

        logger.info(f"Signal for {ticker} ({direction}) passed RiskGuard checks.")
        return True, "Allowed"

    def calculate_sl_tp(self, direction: str, entry_price: float, ticker: str) -> tuple[float, float]:
        """
        Calculates exact Stop-Loss and Take-Profit price levels.
        """
        sl_pct = self.config.get_sl_pct(ticker) # e.g. 0.01 (1%)
        tp_pct = self.config.get_tp_pct(ticker) # e.g. 0.02 (2%)
        
        if direction.upper() == "LONG":
            sl_price = entry_price * (1.0 - sl_pct)
            tp_price = entry_price * (1.0 + tp_pct)
        elif direction.upper() == "SHORT":
            sl_price = entry_price * (1.0 + sl_pct)
            tp_price = entry_price * (1.0 - tp_pct)
        else:
            raise ValueError(f"Invalid direction: {direction}. Must be LONG or SHORT.")

        return sl_price, tp_price

    async def _check_daily_reset(self) -> None:
        """Checks if the calendar day (UTC) has rolled over and resets the daily counter if so."""
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        last_reset = self.state.get_last_reset_date()
        
        if last_reset != today_str:
            logger.info(f"New day detected ({today_str}). Resetting daily trade counter.")
            await self.state.reset_daily_counter(today_str)
