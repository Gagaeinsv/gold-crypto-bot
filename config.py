import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    # Support comma-separated list of channels or bots
    TELEGRAM_CHANNELS = [ch.strip() for ch in os.getenv("TELEGRAM_CHANNEL", "@my_signals_channel").split(",") if ch.strip()]
    TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "trading_parser_session")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_KEY")
    PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
    GROQ_API_KEY = os.getenv("GROQ_KEY", "")  # matches .env variable name
    GROQ_MODEL = os.getenv("GROQ_MODEL", os.getenv("GROQ_MODEL_SIGNALS", "qwen/qwen3-32b"))  # falls back to GROQ_MODEL_SIGNALS from bot.py env
    REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
    DATABASE_PATH = os.getenv("DATABASE_PATH", "users.db")
    
    # Default TP and SL percentages (convert e.g. 2.0 to 0.02)
    DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "2.0")) / 100.0
    DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "1.0")) / 100.0
