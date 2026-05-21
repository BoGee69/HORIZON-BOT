import os
import sqlite3
import json
from pathlib import Path

from config import BASE_DIR, DATA_DIR, DB_PATH, SQLITE_PATH


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip().strip('"').strip("'")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _find_json_source() -> Path | None:
    candidates = [
        _env_path("SOURCE_GAMES_JSON"),
        _env_path("GAMES_JSON_PATH"),
        Path(DB_PATH),
        DATA_DIR / "games.json",
        BASE_DIR / "data" / "games.json",
    ]
    seen: set[str] = set()
    for path in candidates:
        if not path:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_file():
            return path
    return None


JSON_PATH = _find_json_source()
DB_PATH_SQLITE = _env_path("SQLITE_PATH") or Path(SQLITE_PATH)
if not DB_PATH_SQLITE.is_absolute():
    DB_PATH_SQLITE = DATA_DIR / DB_PATH_SQLITE


def migrate():
    if not JSON_PATH:
        print("❌ games.json source not found!")
        print(f"Checked SOURCE_GAMES_JSON, GAMES_JSON_PATH, {DB_PATH}, {DATA_DIR / 'games.json'}, and {BASE_DIR / 'data' / 'games.json'}")
        return

    DB_PATH_SQLITE.parent.mkdir(parents=True, exist_ok=True)

    print(f"🔄 Reading {JSON_PATH}...")
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        items = list(data.values())
    elif isinstance(data, list):
        items = data
    else:
        print("❌ Unsupported games.json format!")
        return

    print(f"📂 Found {len(items)} games. Creating SQLite database at {DB_PATH_SQLITE}...")

    conn = sqlite3.connect(DB_PATH_SQLITE)
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_name ON games(name COLLATE NOCASE)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_has_file ON games(has_file)")

    count = 0
    batch = []
    for item in items:
        if not isinstance(item, dict):
            continue
        appid = str(item.get("appid") or item.get("id") or "").strip()
        if not appid:
            continue
        name = item.get("name")
        has_file = 1 if item.get("file") else 0
        raw_data = json.dumps(item, ensure_ascii=False)
        batch.append((appid, name, has_file, raw_data))
        if len(batch) >= 5000:
            cursor.executemany(
                "INSERT OR REPLACE INTO games (appid, name, has_file, raw_data) VALUES (?, ?, ?, ?)",
                batch,
            )
            conn.commit()
            count += len(batch)
            batch.clear()
            print(f"✅ Migrated {count} games...")

    if batch:
        cursor.executemany(
            "INSERT OR REPLACE INTO games (appid, name, has_file, raw_data) VALUES (?, ?, ?, ?)",
            batch,
        )
        conn.commit()
        count += len(batch)

    conn.close()
    print(f"🎉 Successfully migrated {count} games to {DB_PATH_SQLITE}")


if __name__ == "__main__":
    migrate()
