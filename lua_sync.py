#!/usr/bin/env python3
"""
Steam → R2 Lua ZIP sync

Fetches the Steam app list, compares against R2 inventory (from SQLite cache),
and generates basic Lua ZIP files for any missing games.

Incremental: saves the last processed AppID so each run only catches new games.

Usage:
    python lua_sync.py              # dry-run (no zips created)
    python lua_sync.py --apply      # create missing zips
    python lua_sync.py --apply --limit 1000
    python lua_sync.py --apply --upload  # also upload to R2
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lua_sync")

import config as bot_config
from utils.lua_generator import (
    fetch_all_steam_apps,
    save_lua_zip,
    make_zip_filename,
    load_r2_appids_from_db,
    load_local_appids,
    sanitize_name,
    generate_lua_content,
    create_lua_zip,
)

STATE_FILE = Path(bot_config.DATA_DIR) / "lua_sync_state.json"
LUA_DIR = Path(bot_config.DATA_DIR) / "Lua"
SQLITE_PATH = Path(bot_config.SQLITE_PATH)
STEAM_API_KEY = bot_config.STEAM_API_KEY.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_appid": 0, "total_generated": 0, "last_run": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = time.time()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def sync(apply: bool = False, limit: int = 0, upload: bool = False):
    state = load_state()
    log.info("Loading R2 inventory from SQLite cache...")
    r2_appids = load_r2_appids_from_db(SQLITE_PATH)
    log.info("R2 inventory: %d AppIDs", len(r2_appids))

    local_appids = load_local_appids(LUA_DIR)
    log.info("Local Lua dir: %d zips", len(local_appids))

    known_appids = r2_appids | local_appids
    log.info("Total known AppIDs (R2 + local): %d", len(known_appids))

    log.info("Fetching Steam app list (this may take a while)...")
    all_steam = fetch_all_steam_apps(
        STEAM_API_KEY,
        include_games=bot_config.STEAM_DB_SYNC_INCLUDE_GAMES,
        include_dlc=bot_config.STEAM_DB_SYNC_INCLUDE_DLC,
        include_software=bot_config.STEAM_DB_SYNC_INCLUDE_SOFTWARE,
    )
    log.info("Steam returned %d apps total", len(all_steam))

    # Sort by AppID descending (newest first)
    sorted_apps = sorted(all_steam.items(), key=lambda x: int(x[0]), reverse=True)

    missing = []
    for appid, name in sorted_apps:
        if appid in known_appids:
            continue
        if limit > 0 and len(missing) >= limit:
            break
        missing.append((appid, name))

    log.info("Missing from R2: %d games", len(missing))

    if not missing:
        log.info("Nothing to do!")
        save_state(state)
        return

    # Show preview
    log.info("First 10 missing:")
    for appid, name in missing[:10]:
        zip_name = make_zip_filename(name, appid)
        log.info("  %s  (%s)", zip_name, name)

    if not apply:
        log.info("Dry-run mode. Use --apply to create zip files.")
        return

    # Create zips
    generated = 0
    failed = 0
    new_appids = set()

    for appid, name in missing:
        try:
            dest = save_lua_zip(LUA_DIR, name, appid)
            if dest:
                generated += 1
                new_appids.add(appid)
            else:
                # already exists (race condition)
                pass
        except Exception as exc:
            log.error("Failed to create zip for %s (%s): %s", appid, name, exc)
            failed += 1

        if generated % 500 == 0 and generated > 0:
            log.info("Progress: %d zips created...", generated)

    log.info("Created %d new zip files in %s", generated, LUA_DIR)
    if failed:
        log.warning("Failed: %d", failed)

    # Optional: upload to R2
    if upload and generated > 0:
        try:
            import boto3
            from io import BytesIO

            session = boto3.session.Session()
            client = session.client(
                "s3",
                endpoint_url=f"https://{bot_config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=bot_config.R2_ACCESS_KEY_ID,
                aws_secret_access_key=bot_config.R2_SECRET_ACCESS_KEY,
                region_name="auto",
            )

            uploaded = 0
            for appid in new_appids:
                try:
                    zip_path = LUA_DIR / make_zip_filename(all_steam.get(appid, appid), appid)
                    if zip_path.exists():
                        r2_key = f"Database/{zip_path.name}"
                        client.put_object(
                            Bucket=bot_config.R2_BUCKET_NAME,
                            Key=r2_key,
                            Body=zip_path.read_bytes(),
                            ContentType="application/zip",
                        )
                        uploaded += 1
                except Exception as exc:
                    log.error("Upload failed for %s: %s", appid, exc)

                if uploaded % 200 == 0 and uploaded > 0:
                    log.info("Uploaded %d to R2...", uploaded)

            log.info("Uploaded %d zips to R2", uploaded)
        except Exception as exc:
            log.error("R2 upload failed: %s", exc)

    # Save state for incremental next run
    state["total_generated"] = state.get("total_generated", 0) + generated
    save_state(state)
    log.info("State saved. Total generated all time: %d", state["total_generated"])


def main():
    parser = argparse.ArgumentParser(description="Generate missing Lua zips for Steam apps not in R2")
    parser.add_argument("--apply", action="store_true", help="Actually create zip files")
    parser.add_argument("--limit", type=int, default=0, help="Max zips to create (0 = unlimited)")
    parser.add_argument("--upload", action="store_true", help="Also upload new zips to R2")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    LUA_DIR.mkdir(parents=True, exist_ok=True)
    sync(apply=args.apply, limit=args.limit, upload=args.upload)


if __name__ == "__main__":
    main()
