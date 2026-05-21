import sqlite3
import json
from pathlib import Path

# Path config
JSON_PATH = Path("data/games.json")
DB_PATH = Path("data/games.db")

def migrate():
    if not JSON_PATH.exists():
        print("❌ games.json not found!")
        return

    print(f"🔄 Reading {JSON_PATH}...")
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"📂 Found {len(data)} games. Creating SQLite database...")
    
    # Connect to SQLite
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        appid TEXT PRIMARY KEY,
        name TEXT,
        has_file BOOLEAN,
        raw_data TEXT
    )
    """)

    # Insert data
    count = 0
    for item in data:
        appid = str(item.get("appid") or item.get("id") or "")
        name = item.get("name")
        has_file = 1 if item.get("file") else 0
        raw_data = json.dumps(item)
        
        if appid:
            cursor.execute(
                "INSERT OR REPLACE INTO games (appid, name, has_file, raw_data) VALUES (?, ?, ?, ?)",
                (appid, name, has_file, raw_data)
            )
            count += 1
            if count % 5000 == 0:
                print(f"✅ Migrated {count} games...")

    conn.commit()
    conn.close()
    print(f"🎉 Successfully migrated {count} games to {DB_PATH}")

if __name__ == "__main__":
    migrate()
