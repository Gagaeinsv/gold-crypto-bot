import logging
from services.video_engine import VideoEngine

logging.basicConfig(level=logging.INFO)

trade_data = {
    "id": 888,
    "asset": "XAUUSD",
    "direction": "SELL",
    "pnl_percentage": 25.10
}

metrics = {
    "win_rate": 68.5
}

print("Starting test video generation...")
output = VideoEngine.generate_shorts(trade_data, metrics)
print(f"Result: {output}")
