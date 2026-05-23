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
from config import SQLITE_PATH as CONFIG_SQLITE_PATH

log = logging.getLogger(__name__)


def _resolve_sqlite_path() -> Path:
    return Path(CONFIG_SQLITE_PATH)


SQLITE_PATH = _resolve_sqlite_path()
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
        # Index for fast name/appid search used by autocomplete
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_name ON games(name COLLATE NOCASE)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_has_file ON games(has_file)")
        conn.commit()
        self._warn_if_empty(conn)
        conn.close()

    def _warn_if_empty(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM games")
        existing = cursor.fetchone()[0]
        if existing == 0:
            log.warning(
                "SQLite games table is empty at %s. Steam DB sync can populate it; "
                "OpenDir sync will wait until valid appid/name rows exist.",
                self.db_path,
            )

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
            log.info(f"✅ SQLite Database ready at {self.db_path} with {count:,} games (WAL mode active)")
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
        """Search games by name or AppID. Empty query returns starred games first."""
        results = []
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            if query:
                cursor.execute(
                    """SELECT raw_data FROM games
                       WHERE name LIKE ? OR appid LIKE ?
                       ORDER BY has_file DESC, name ASC
                       LIMIT ?""",
                    (f"%{query}%", f"%{query}%", limit)
                )
            else:
                cursor.execute(
                    """SELECT raw_data FROM games
                       WHERE name IS NOT NULL AND name != ''
                       ORDER BY has_file DESC, name ASC
                       LIMIT ?""",
                    (limit,)
                )
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                results.append(json.loads(row[0]))
        except Exception as e:
            log.error(f"Search failed: {e}")
        return results

    def autocomplete_titles(self, query: str, limit: int = 25) -> List[Dict]:
        """Autocomplete game titles from SQLite only."""
        results = []
        limit = max(1, min(int(limit or 25), 25))
        query = " ".join(str(query or "").strip().split())
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            if query:
                cursor.execute(
                    """SELECT appid, name, has_file, raw_data FROM games
                       WHERE name IS NOT NULL
                         AND name != ''
                         AND name LIKE ?
                       ORDER BY
                         CASE WHEN name LIKE ? THEN 0 ELSE 1 END,
                         has_file DESC,
                         name COLLATE NOCASE ASC
                       LIMIT ?""",
                    (f"%{query}%", f"{query}%", limit),
                )
            else:
                cursor.execute(
                    """SELECT appid, name, has_file, raw_data FROM games
                       WHERE name IS NOT NULL AND name != ''
                       ORDER BY has_file DESC, name COLLATE NOCASE ASC
                       LIMIT ?""",
                    (limit,),
                )
            rows = cursor.fetchall()
            conn.close()
            for appid, name, has_file, raw_data in rows:
                try:
                    item = json.loads(raw_data) if raw_data else {}
                except Exception:
                    item = {}
                item["appid"] = str(item.get("appid") or item.get("id") or appid)
                item["name"] = item.get("name") or name
                item["file"] = bool(item.get("file") or has_file)
                results.append(item)
        except Exception as e:
            log.error(f"SQLite title autocomplete failed: {e}")
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


# ─────────────────────────────────────────────
# R2 Inventory cache stored inside SQLite
# ─────────────────────────────────────────────

class R2InventoryDB:
    """
    Stores R2 object keys inside SQLite so OpenDir never needs to call
    list_objects_v2 during a sync run.

    Schema
    ------
    r2_inventory(key TEXT PRIMARY KEY, size INTEGER, last_modified TEXT, synced_at REAL)

    Typical usage
    -------------
    1. On bot start (or on a schedule), call ``rebuild(s3_client, bucket, prefix)``
       to do a full R2 → SQLite snapshot.
    2. In OpenDir, replace ``_list_r2_keys()`` with ``contains(key)`` /
       ``get_all_keys()`` which are O(1) / O(n) SQLite queries.
    3. After every successful upload, call ``mark_uploaded(key)`` so the local
       cache stays accurate without needing another full rebuild.
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or SQLITE_PATH
        self._init_table()

    def _init_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS r2_inventory (
                    key           TEXT PRIMARY KEY,
                    size          INTEGER,
                    last_modified TEXT,
                    synced_at     REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_r2_key ON r2_inventory(key)")
            conn.commit()

    # ------------------------------------------------------------------
    # Rebuild (full sync from R2)
    # ------------------------------------------------------------------

    def rebuild(self, s3_client, bucket: str, prefix: str = "") -> dict:
        """
        Full-scan R2 bucket/prefix and store every key in SQLite.
        Returns a summary dict with counts and timing.
        """
        import time
        started = time.time()
        keys_found: list[tuple] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                k = obj.get("Key") or ""
                if not k:
                    continue
                keys_found.append((
                    k,
                    int(obj.get("Size") or 0),
                    str(obj.get("LastModified") or ""),
                    time.time(),
                ))

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # Wipe stale data for this prefix then bulk-insert
            conn.execute("DELETE FROM r2_inventory WHERE key LIKE ?", (f"{prefix}%",))
            conn.executemany(
                "INSERT OR REPLACE INTO r2_inventory (key, size, last_modified, synced_at) VALUES (?,?,?,?)",
                keys_found,
            )
            conn.commit()

        elapsed = time.time() - started
        log.info(
            "R2InventoryDB: rebuilt %d keys under prefix=%r in %.1fs",
            len(keys_found), prefix, elapsed,
        )
        return {
            "keys_synced": len(keys_found),
            "prefix": prefix,
            "elapsed_seconds": round(elapsed, 2),
        }

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def contains(self, key: str) -> bool:
        """O(1) primary-key lookup."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM r2_inventory WHERE key = ? LIMIT 1", (key,)
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def find_zip_by_appid(self, prefix: str = "", appid: str = "") -> Optional[Dict]:
        """Find any cached ZIP object whose filename points at the given AppID."""
        appid = str(appid or "").strip()
        if not appid.isdigit():
            return None
        prefix = str(prefix or "").lstrip("/")
        patterns = []
        if prefix:
            patterns.extend([
                f"{prefix}%({appid}).zip",
                f"{prefix}{appid}.zip",
                f"{prefix}[{appid}].zip",
            ])
        patterns.extend([
            f"%({appid}).zip",
            f"{appid}.zip",
            f"[{appid}].zip",
        ])
        try:
            with sqlite3.connect(self.db_path) as conn:
                for pattern in patterns:
                    row = conn.execute(
                        """
                        SELECT key, size, last_modified
                        FROM r2_inventory
                        WHERE key LIKE ?
                        ORDER BY
                            CASE
                                WHEN key LIKE ? THEN 0
                                WHEN key LIKE ? THEN 1
                                ELSE 2
                            END,
                            key ASC
                        LIMIT 1
                        """,
                        (pattern, f"{prefix}%({appid}).zip", f"%({appid}).zip"),
                    ).fetchone()
                    if row:
                        return {
                            "key": row[0],
                            "size": int(row[1] or 0),
                            "last_modified": str(row[2] or ""),
                        }
        except Exception:
            return None
        return None

    def get_all_keys(self, prefix: str = "") -> set[str]:
        """Return all cached keys, optionally filtered by prefix."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                if prefix:
                    rows = conn.execute(
                        "SELECT key FROM r2_inventory WHERE key LIKE ?",
                        (f"{prefix}%",),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT key FROM r2_inventory").fetchall()
                return {r[0] for r in rows}
        except Exception:
            return set()

    def count(self, prefix: str = "") -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                if prefix:
                    return conn.execute(
                        "SELECT COUNT(*) FROM r2_inventory WHERE key LIKE ?",
                        (f"{prefix}%",),
                    ).fetchone()[0]
                return conn.execute("SELECT COUNT(*) FROM r2_inventory").fetchone()[0]
        except Exception:
            return 0

    def last_synced_at(self, prefix: str = "") -> float:
        """Return the oldest synced_at timestamp for the given prefix (0 if empty)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT MIN(synced_at) FROM r2_inventory WHERE key LIKE ?",
                    (f"{prefix}%",),
                ).fetchone()
                return float(row[0] or 0)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Mutation helpers (keep cache fresh after uploads/deletes)
    # ------------------------------------------------------------------

    def mark_uploaded(self, key: str, size: int = 0) -> None:
        """Call after a successful upload so the cache reflects the new key."""
        import time
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO r2_inventory (key, size, last_modified, synced_at) VALUES (?,?,?,?)",
                    (key, size, "", time.time()),
                )
                conn.commit()
        except Exception as exc:
            log.warning("R2InventoryDB.mark_uploaded failed: %r", exc)

    def mark_deleted(self, key: str) -> None:
        """Call after a successful delete."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM r2_inventory WHERE key = ?", (key,))
                conn.commit()
        except Exception as exc:
            log.warning("R2InventoryDB.mark_deleted failed: %r", exc)
