import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # --- Exchange Settings ---
    EXCHANGE_ID = os.getenv("EXCHANGE_ID", "bybit").lower()
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET_KEY = os.getenv("EXCHANGE_SECRET_KEY", "")
    IS_SANDBOX = os.getenv("EXCHANGE_IS_SANDBOX", "true").lower() == "true" # Defaults to testnet/sandbox
    
    # --- Risk Settings ---
    MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "10"))
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "1"))
    
    # Fixed SL/TP percentages (e.g. 1.0% Stop-Loss, 2.0% Take-Profit)
    DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.01"))
    DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.02"))
    
    # Position sizing
    DEFAULT_SIZE_USD = float(os.getenv("DEFAULT_SIZE_USD", "50.0")) # Size in USD per scalp trade
    
    # --- Server Settings ---
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8000"))

    @classmethod
    def get_sl_pct(cls, ticker: str) -> float:
        # Can be customized per ticker in the future
        return cls.DEFAULT_SL_PCT

    @classmethod
    def get_tp_pct(cls, ticker: str) -> float:
        # Can be customized per ticker in the future
        return cls.DEFAULT_TP_PCT
