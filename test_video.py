import logging
from services.video_engine import VideoEngine

logging.basicConfig(level=logging.INFO)

trade_data = {
    "id": 999,
    "asset": "BTCUSDT",
    "direction": "BUY",
    "pnl_percentage": 15.42
}

metrics = {
    "win_rate": 68.5
}

print("Starting test video generation...")
output = VideoEngine.generate_shorts(trade_data, metrics)
print(f"Result: {output}")
