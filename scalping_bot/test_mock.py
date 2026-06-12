import os
import asyncio
import logging
from config import Config
from state import StateManager
from risk import RiskGuard

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("test_mock")

# Custom test config
class TestConfig(Config):
    MAX_DAILY_TRADES = 3
    MAX_CONCURRENT_TRADES = 1
    DEFAULT_SL_PCT = 0.01
    DEFAULT_TP_PCT = 0.02

async def run_tests():
    test_file = "test_state.json"
    if os.path.exists(test_file):
        os.remove(test_file)

    logger.info("Initializing StateManager and RiskGuard...")
    state = StateManager(test_file)
    risk = RiskGuard(state, TestConfig)

    # Test Case 1: Initial state is clean
    logger.info("--- Test Case 1: Clean State ---")
    allowed, reason = await risk.can_trade("SOL/USDT", "LONG")
    assert allowed is True, f"Failed Case 1: {reason}"
    logger.info(f"OK: allowed={allowed}, reason={reason}")

    # Test Case 2: Max Concurrent Trades restriction
    logger.info("--- Test Case 2: Concurrent Trade Restriction ---")
    await state.set_active_trade("SOL/USDT")
    allowed, reason = await risk.can_trade("BTC/USDT", "SHORT")
    assert allowed is False, f"Failed Case 2: Allowed trade despite concurrent trade open."
    assert "Active concurrent trade open" in reason, f"Failed Case 2 reason: {reason}"
    logger.info(f"OK: Blocked concurrent position. reason={reason}")

    # Clear position and increment daily trade count
    await state.set_active_trade(None)
    
    # Test Case 3: Incremented daily trades under limit
    logger.info("--- Test Case 3: Daily Trades Under Limit ---")
    await state.increment_daily_trade_count() # 1
    await state.increment_daily_trade_count() # 2
    allowed, reason = await risk.can_trade("SOL/USDT", "LONG")
    assert allowed is True, f"Failed Case 3: Blocked under limit. count={state.get_daily_trade_count()}"
    logger.info(f"OK: Allowed under daily limit. count={state.get_daily_trade_count()}")

    # Test Case 4: Max Daily Trades limit hit
    logger.info("--- Test Case 4: Daily Limit Hit ---")
    await state.increment_daily_trade_count() # 3 (Hits limit of 3)
    allowed, reason = await risk.can_trade("SOL/USDT", "LONG")
    assert allowed is False, f"Failed Case 4: Allowed trade past daily limit. count={state.get_daily_trade_count()}"
    assert "Daily trade limit reached" in reason, f"Failed Case 4 reason: {reason}"
    logger.info(f"OK: Blocked at daily limit. count={state.get_daily_trade_count()}")

    # Test Case 5: UTC Date Rollover resets counter
    logger.info("--- Test Case 5: Daily UTC Rollover Reset ---")
    # Manually backdate the reset date in the state file
    state.state["last_reset_date"] = "2000-01-01"
    await state.save()
    
    allowed, reason = await risk.can_trade("SOL/USDT", "LONG")
    assert allowed is True, f"Failed Case 5: Blocked after rollover. count={state.get_daily_trade_count()}"
    assert state.get_daily_trade_count() == 0, f"Failed Case 5: Counter did not reset to 0."
    logger.info("OK: Rollover successfully reset daily trade counter.")

    # Test Case 6: Stop-Loss and Take-Profit calculations
    logger.info("--- Test Case 6: SL/TP Calculations ---")
    # LONG: Entry=100.0, SL=1%, TP=2% => SL=99.0, TP=102.0
    sl_long, tp_long = risk.calculate_sl_tp("LONG", 100.0, "SOL/USDT")
    assert sl_long == 99.0, f"LONG SL Calculation failed: {sl_long}"
    assert tp_long == 102.0, f"LONG TP Calculation failed: {tp_long}"
    
    # SHORT: Entry=100.0, SL=1%, TP=2% => SL=101.0, TP=98.0
    sl_short, tp_short = risk.calculate_sl_tp("SHORT", 100.0, "SOL/USDT")
    assert sl_short == 101.0, f"SHORT SL Calculation failed: {sl_short}"
    assert tp_short == 98.0, f"SHORT TP Calculation failed: {tp_short}"
    logger.info(f"OK: Calculations matching. LONG SL={sl_long}/TP={tp_long}, SHORT SL={sl_short}/TP={tp_short}")

    # Clean up test file
    if os.path.exists(test_file):
        os.remove(test_file)
    logger.info("\nALL TESTS PASSED SUCCESSFULLY! RISK GUARD IS IRONCLAD.")

if __name__ == "__main__":
    asyncio.run(run_tests())
