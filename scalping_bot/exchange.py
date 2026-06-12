import asyncio
import logging
import ccxt.pro as ccxtpro
from config import Config
from state import StateManager

logger = logging.getLogger("scalping_bot.exchange")

class ExchangeManager:
    def __init__(self, state_manager: StateManager, config: Config = Config):
        self.state = state_manager
        self.config = config
        
        # Initialize exchange client (Bybit Futures / Unified)
        exchange_class = getattr(ccxtpro, self.config.EXCHANGE_ID)
        exchange_params = {
            "apiKey": self.config.API_KEY,
            "secret": self.config.SECRET_KEY,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # Set to perpetual swaps (futures) by default
            }
        }
        self.exchange = exchange_class(exchange_params)
        
        # Set sandbox/testnet environment
        if self.config.IS_SANDBOX:
            self.exchange.set_sandbox_mode(True)
            logger.info("Exchange initialized in Sandbox/Testnet mode.")
        else:
            logger.info("Exchange initialized in LIVE mode.")
            
        self._markets_loaded = False

    async def load_markets(self):
        """Loads exchange markets if not already loaded."""
        if not self._markets_loaded:
            logger.info("Loading exchange markets...")
            await self.exchange.load_markets()
            self._markets_loaded = True

    async def close(self):
        """Closes the exchange websocket connections."""
        await self.exchange.close()

    async def open_position(self, ticker: str, direction: str, size_usd: float) -> tuple[float, float]:
        """
        Opens a position by placing a MARKET order.
        Returns:
            (average_fill_price, filled_amount)
        """
        await self.load_markets()
        
        # 1. Fetch current price to estimate the amount
        ticker_info = await self.exchange.fetch_ticker(ticker)
        current_price = ticker_info.get("last")
        if not current_price:
            raise ValueError(f"Could not fetch current price for {ticker}")
        
        # Calculate size amount based on leverage (assuming 1x leverage size_usd or handle size calculation)
        raw_amount = size_usd / current_price
        amount = float(self.exchange.amount_to_precision(ticker, raw_amount))
        
        side = "buy" if direction.upper() == "LONG" else "sell"
        
        logger.info(f"Opening position on {ticker} | Side: {side.upper()} | Size USD: ${size_usd} | Est. Amount: {amount}")
        
        # 2. Place market order
        order = await self.exchange.create_order(
            symbol=ticker,
            type="market",
            side=side,
            amount=amount
        )
        
        # Extract execution details
        fill_price = order.get("average") or order.get("price") or current_price
        filled_amount = order.get("filled") or amount
        
        logger.info(f"Position opened on {ticker}. Fill Price: {fill_price} | Filled Amount: {filled_amount}")
        return float(fill_price), float(filled_amount)

    async def place_protective_orders(
        self, ticker: str, direction: str, entry_price: float, size: float, sl_price: float, tp_price: float
    ) -> tuple[str, str]:
        """
        Places linked Stop-Loss (STOP_MARKET) and Take-Profit (LIMIT) orders.
        Returns:
            (sl_order_id, tp_order_id)
        """
        await self.load_markets()
        
        # Exit side is opposite to entry side
        exit_side = "sell" if direction.upper() == "LONG" else "buy"
        
        # Format prices to match exchange requirements
        sl_price_formatted = float(self.exchange.price_to_precision(ticker, sl_price))
        tp_price_formatted = float(self.exchange.price_to_precision(ticker, tp_price))
        
        logger.info(
            f"Placing protective orders for {ticker} | Exit Side: {exit_side.upper()} | Size: {size}\n"
            f"SL Trigger Price: {sl_price_formatted} | TP Limit Price: {tp_price_formatted}"
        )
        
        # Place Stop-Loss Order (Stop Market)
        # Note: Bybit expects triggerPrice or stopPrice in params
        sl_params = {
            "reduceOnly": True,
            "triggerPrice": sl_price_formatted
        }
        
        # Place Take-Profit Order (Limit Order)
        tp_params = {
            "reduceOnly": True
        }
        
        # Execute orders
        # Place TP order first (Limit)
        tp_order = await self.exchange.create_order(
            symbol=ticker,
            type="limit",
            side=exit_side,
            amount=size,
            price=tp_price_formatted,
            params=tp_params
        )
        
        # Place SL order (Stop Market)
        sl_order = await self.exchange.create_order(
            symbol=ticker,
            type="market",
            side=exit_side,
            amount=size,
            params=sl_params
        )
        
        sl_id = sl_order.get("id")
        tp_id = tp_order.get("id")
        
        logger.info(f"Protective orders placed successfully. SL ID: {sl_id} | TP ID: {tp_id}")
        return sl_id, tp_id

    async def cancel_protective_orders(self, ticker: str, sl_id: str | None, tp_id: str | None):
        """Cancels remaining protective orders once one gets filled."""
        await self.load_markets()
        
        if sl_id:
            try:
                logger.info(f"Canceling Stop-Loss order: {sl_id}")
                await self.exchange.cancel_order(sl_id, ticker)
            except Exception as e:
                logger.warning(f"Failed to cancel SL order {sl_id} (might be already filled/canceled): {e}")
                
        if tp_id:
            try:
                logger.info(f"Canceling Take-Profit order: {tp_id}")
                await self.exchange.cancel_order(tp_id, ticker)
            except Exception as e:
                logger.warning(f"Failed to cancel TP order {tp_id} (might be already filled/canceled): {e}")

    async def watch_and_manage_position(
        self, ticker: str, sl_id: str, tp_id: str
    ):
        """
        WebSocket listener loop that monitors private order streams.
        If Stop-Loss hits, cancels Take-Profit (and vice-versa), then resets active position in state.
        """
        logger.info(f"Starting WS order stream monitor for {ticker} | SL: {sl_id} | TP: {tp_id}")
        
        try:
            while self.state.has_active_trade():
                # Watch order updates via websocket stream
                orders = await self.exchange.watch_orders(symbol=ticker)
                
                for order in orders:
                    order_id = order.get("id")
                    status = order.get("status")
                    
                    if order_id == sl_id and status in ("closed", "filled"):
                        logger.info(f"🎯 Stop-Loss filled ({sl_id}). Closing position and canceling TP...")
                        # Cancel remaining TP order
                        await self.cancel_protective_orders(ticker, sl_id=None, tp_id=tp_id)
                        # Reset active position state
                        await self.state.set_active_trade(None)
                        return
                        
                    elif order_id == tp_id and status in ("closed", "filled"):
                        logger.info(f"🎉 Take-Profit filled ({tp_id}). Closing position and canceling SL...")
                        # Cancel remaining SL order
                        await self.cancel_protective_orders(ticker, sl_id=sl_id, tp_id=None)
                        # Reset active position state
                        await self.state.set_active_trade(None)
                        return
                        
                # Sleep briefly to avoid tight CPU loops
                await asyncio.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Error in WS watch_and_manage_position: {e}")
            # Fallback check: query order status via REST if WS crashes
            await self._rest_fallback_monitor(ticker, sl_id, tp_id)

    async def _rest_fallback_monitor(self, ticker: str, sl_id: str, tp_id: str):
        """Fallback REST API polling monitor in case WS stream fails."""
        logger.info(f"Starting REST fallback polling monitor for {ticker}")
        
        while self.state.has_active_trade():
            try:
                # Poll order statuses
                sl_order = await self.exchange.fetch_order(sl_id, ticker)
                tp_order = await self.exchange.fetch_order(tp_id, ticker)
                
                if sl_order.get("status") in ("closed", "filled"):
                    logger.info("REST: Stop-Loss filled. Canceling TP...")
                    await self.cancel_protective_orders(ticker, sl_id=None, tp_id=tp_id)
                    await self.state.set_active_trade(None)
                    return
                elif tp_order.get("status") in ("closed", "filled"):
                    logger.info("REST: Take-Profit filled. Canceling SL...")
                    await self.cancel_protective_orders(ticker, sl_id=sl_id, tp_id=None)
                    await self.state.set_active_trade(None)
                    return
                    
            except Exception as e:
                logger.error(f"REST fallback error: {e}")
                
            await asyncio.sleep(5.0) # Poll every 5s
