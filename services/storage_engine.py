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
            # Ensure the signals table exists in case of a new environment
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair         TEXT    NOT NULL,
                    direction    TEXT    NOT NULL,
                    entry_price  REAL    NOT NULL,
                    sl_price     REAL    NOT NULL,
                    tp_price     REAL    NOT NULL,
                    score        INTEGER DEFAULT 0,
                    sentiment    TEXT    DEFAULT 'neutral',
                    source       TEXT    DEFAULT 'ai',
                    posted_at    TEXT    DEFAULT (datetime('now')),
                    resolved_at  TEXT,
                    outcome      TEXT,
                    pnl_pct      REAL,
                    message_id   INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    def save_trade(self, asset: str, direction: str, entry_price: float) -> int:
        asset = asset.upper().strip()
        direction = direction.upper().strip()
        
        # Calculate sl and tp prices using Config defaults
        tp_pct = Config.DEFAULT_TP_PCT
        sl_pct = Config.DEFAULT_SL_PCT
        
        if direction == "BUY":
            tp_price = entry_price * (1.0 + tp_pct)
            sl_price = entry_price * (1.0 - sl_pct)
        else:
            tp_price = entry_price * (1.0 - tp_pct)
            sl_price = entry_price * (1.0 + sl_pct)
            
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signals (pair, direction, entry_price, sl_price, tp_price, posted_at, source)
                VALUES (?, ?, ?, ?, ?, ?, 'manual')
            """, (asset, direction, entry_price, sl_price, tp_price, now_str))
            conn.commit()
            return cursor.lastrowid

    def get_open_trades(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE outcome IS NULL")
            rows = cursor.fetchall()
            
            res = []
            for row in rows:
                res.append({
                    "id": row["id"],
                    "asset": row["pair"],
                    "direction": row["direction"],
                    "entry_price": row["entry_price"],
                    "exit_price": None,
                    "pnl_percentage": None,
                    "status": "OPEN",
                    "created_at": row["posted_at"],
                    "closed_at": None
                })
            return res

    def close_trade(self, trade_id: int, exit_price: float, pnl_percentage: float):
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        outcome = "TP" if pnl_percentage > 0 else "SL"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals
                SET outcome = ?, pnl_pct = ?, resolved_at = ?
                WHERE id = ?
            """, (outcome, pnl_percentage, now_str, trade_id))
            conn.commit()

    def get_all_trades(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals ORDER BY posted_at DESC")
            rows = cursor.fetchall()
            
            res = []
            for row in rows:
                pnl = row["pnl_pct"]
                outcome = row["outcome"]
                direction = row["direction"]
                entry = row["entry_price"]
                
                # Reconstruct exit price mathematically based on entry and PnL if resolved
                exit_price = None
                if pnl is not None:
                    if direction == "BUY":
                        exit_price = entry * (1.0 + (pnl / 100.0))
                    else: # SELL
                        exit_price = entry * (1.0 - (pnl / 100.0))
                
                res.append({
                    "id": row["id"],
                    "asset": row["pair"],
                    "direction": direction,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "pnl_percentage": pnl,
                    "status": "OPEN" if outcome is None else "CLOSED",
                    "created_at": row["posted_at"],
                    "closed_at": row["resolved_at"],
                    "source": row["source"]
                })
            return res

    def get_metrics(self, source: str = None) -> dict:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Helper to append source filter
            source_cond = " AND source = ?" if source else ""
            params = (source,) if source else ()
            
            # 1. Overall Win Rate
            cursor.execute(f"SELECT COUNT(*) FROM signals WHERE outcome IS NOT NULL{source_cond}", params)
            total_closed = cursor.fetchone()[0]
            
            if total_closed > 0:
                # Wins are resolutions with positive PnL
                cursor.execute(f"SELECT COUNT(*) FROM signals WHERE outcome IS NOT NULL AND pnl_pct > 0{source_cond}", params)
                wins = cursor.fetchone()[0]
                win_rate = (wins / total_closed) * 100.0
            else:
                win_rate = 0.0

            # 2. 7-Day Cumulative PnL
            seven_days_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            query_params = (seven_days_ago,) + params
            cursor.execute(f"""
                SELECT SUM(pnl_pct) FROM signals 
                WHERE outcome IS NOT NULL AND resolved_at >= ?{source_cond}
            """, query_params)
            res_pnl = cursor.fetchone()[0]
            cumulative_weekly_pnl = float(res_pnl) if res_pnl is not None else 0.0

            # 3. Active Winning Streak
            cursor.execute(f"""
                SELECT pnl_pct FROM signals 
                WHERE outcome IS NOT NULL{source_cond}
                ORDER BY resolved_at DESC, id DESC
            """, params)
            closed_pnl_list = [row['pnl_pct'] for row in cursor.fetchall()]
            
            win_streak = 0
            for pnl in closed_pnl_list:
                if pnl is not None and pnl > 0:
                    win_streak += 1
                else:
                    break

            return {
                "win_rate": round(win_rate, 2),
                "cumulative_weekly_pnl": round(cumulative_weekly_pnl, 2),
                "win_streak": win_streak
            }
