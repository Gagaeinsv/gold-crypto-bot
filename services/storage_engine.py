import os
import sqlite3
from datetime import datetime, timedelta
from config import Config

class StorageEngine:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.DATABASE_PATH
        # Ensure the directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    pnl_percentage REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                )
            """)
            conn.commit()

    def save_trade(self, asset: str, direction: str, entry_price: float) -> int:
        asset = asset.upper().strip()
        direction = direction.upper().strip()
        # Auto-detect timestamps
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (asset, direction, entry_price, status, created_at)
                VALUES (?, ?, ?, 'OPEN', ?)
            """, (asset, direction, entry_price, now_str))
            conn.commit()
            return cursor.lastrowid

    def get_open_trades(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
            return [dict(row) for row in cursor.fetchall()]

    def close_trade(self, trade_id: int, exit_price: float, pnl_percentage: float):
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades
                SET exit_price = ?, pnl_percentage = ?, status = 'CLOSED', closed_at = ?
                WHERE id = ?
            """, (exit_price, pnl_percentage, now_str, trade_id))
            conn.commit()

    def get_all_trades(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_metrics(self) -> dict:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Overall Win Rate
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'CLOSED'")
            total_closed = cursor.fetchone()[0]
            
            if total_closed > 0:
                cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'CLOSED' AND pnl_percentage > 0")
                wins = cursor.fetchone()[0]
                win_rate = (wins / total_closed) * 100.0
            else:
                win_rate = 0.0

            # 2. 7-Day Cumulative PnL
            seven_days_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                SELECT SUM(pnl_percentage) FROM trades 
                WHERE status = 'CLOSED' AND closed_at >= ?
            """, (seven_days_ago,))
            res_pnl = cursor.fetchone()[0]
            cumulative_weekly_pnl = float(res_pnl) if res_pnl is not None else 0.0

            # 3. Active Winning Streak
            cursor.execute("""
                SELECT pnl_percentage FROM trades 
                WHERE status = 'CLOSED' 
                ORDER BY closed_at DESC, id DESC
            """)
            closed_pnl_list = [row['pnl_percentage'] for row in cursor.fetchall()]
            
            win_streak = 0
            for pnl in closed_pnl_list:
                if pnl > 0:
                    win_streak += 1
                else:
                    break  # Streak breaks on first non-win

            return {
                "win_rate": round(win_rate, 2),
                "cumulative_weekly_pnl": round(cumulative_weekly_pnl, 2),
                "win_streak": win_streak
            }
