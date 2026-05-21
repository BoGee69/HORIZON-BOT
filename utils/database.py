"""
Database manager for game database operations (SQLite Version)
Handles SQLite storage, indexing, and backup management
"""
import sqlite3
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional
from config import DATA_DIR, ENABLE_AUTO_BACKUP, MAX_BACKUPS

log = logging.getLogger(__name__)
SQLITE_PATH = DATA_DIR / "games.db"
PLACEHOLDER_RE = re.compile(r"^game\s+\d+$", re.IGNORECASE)

def is_placeholder_game_name(name: Optional[str], appid: str) -> bool:
    if not name:
        return True
    clean = " ".join(str(name).strip().split())
    return clean == str(appid) or PLACEHOLDER_RE.match(clean) is not None

class DatabaseManager:
    """Manages game database with SQLite for better performance"""
    
    def __init__(self):
        self.db_path = SQLITE_PATH
        self._init_db()
        
    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        # Enable WAL mode for better concurrency and crash resistance
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS games (
            appid TEXT PRIMARY KEY,
            name TEXT,
            has_file BOOLEAN,
            raw_data TEXT
        )
        """)
        conn.commit()
        conn.close()

    def load(self) -> bool:
        """Verify database connection and integrity"""
        try:
            conn = sqlite3.connect(self.db_path)
            # Perform a quick integrity check
            integrity = conn.execute("PRAGMA integrity_check(1)").fetchone()[0]
            if integrity != "ok":
                log.error(f"❌ Database corruption detected: {integrity}")
                conn.close()
                return False
                
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM games")
            count = cursor.fetchone()[0]
            conn.close()
            log.info(f"✅ SQLite Database ready with {count:,} games (WAL mode active)")
            return True
        except Exception as e:
            log.error(f"Error connecting to SQLite: {e}")
            return False
    
    def save(self) -> bool:
        """SQLite commits automatically on changes, but we keep this for compatibility"""
        return True
    
    def add_game(self, appid: str, name: Optional[str] = None, has_file: bool = False) -> bool:
        """Add a game to database"""
        appid_str = str(appid)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            raw_data = json.dumps({"appid": appid_str, "name": name, "file": has_file})
            cursor.execute(
                "INSERT OR IGNORE INTO games (appid, name, has_file, raw_data) VALUES (?, ?, ?, ?)",
                (appid_str, name, 1 if has_file else 0, raw_data)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error(f"Failed to add game to SQLite: {e}")
            return False
    
    def update_game(self, appid: str, **kwargs) -> bool:
        """Update game entry"""
        appid_str = str(appid)
        game = self.get_game(appid_str)
        if not game:
            return False
        
        game.update(kwargs)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE games SET name = ?, has_file = ?, raw_data = ? WHERE appid = ?",
                (game.get("name"), 1 if game.get("file") else 0, json.dumps(game), appid_str)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error(f"Failed to update game in SQLite: {e}")
            return False
    
    def mark_as_starred(self, appid: str, name: Optional[str] = None) -> bool:
        """Mark game as having file available"""
        return self.update_game(appid, file=True, name=name) if self.get_game(appid) else self.add_game(appid, name, has_file=True)
    
    def get_game(self, appid: str) -> Optional[Dict]:
        """Get game by AppID"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT raw_data FROM games WHERE appid = ?", (str(appid),))
            row = cursor.fetchone()
            conn.close()
            return json.loads(row[0]) if row else None
        except Exception:
            return None
    
    def search_games(self, query: str, limit: int = 25) -> List[Dict]:
        """Search games by name or AppID using SQL"""
        results = []
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT raw_data FROM games WHERE name LIKE ? OR appid LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)
            )
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                results.append(json.loads(row[0]))
        except Exception as e:
            log.error(f"Search failed: {e}")
        return results
    
    def get_stats(self) -> Dict:
        """Get database statistics using SQL"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM games")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM games WHERE has_file = 1")
            with_files = cursor.fetchone()[0]
            cursor.execute("SELECT MAX(CAST(appid AS INTEGER)) FROM games WHERE appid GLOB '[0-9]*'")
            last_appid = cursor.fetchone()[0] or 0
            conn.close()
            return {
                "total": total,
                "with_files": with_files,
                "with_names": total, # In SQLite version we assume all have names if they exist
                "last_appid": last_appid
            }
        except Exception:
            return {"total": 0, "with_files": 0, "with_names": 0, "last_appid": 0}
