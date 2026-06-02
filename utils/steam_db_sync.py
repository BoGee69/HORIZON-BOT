"""
Steam app list database sync (SQLite Compatible).

Fills placeholder names in games.db and can add newly published Steam app IDs.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from typing import Optional

import config as bot_config
from utils.database import DatabaseManager, is_placeholder_game_name

log = logging.getLogger(__name__)

PUBLIC_STEAM_APP_LIST_URLS = [
    "https://api.steampowered.com/ISteamApps/GetAppList/v2/?format=json",
    "https://api.steampowered.com/ISteamApps/GetAppList/v0002/?format=json",
]
STEAM_STORE_SERVICE_URL = "https://partner.steam-api.com/IStoreService/GetAppList/v1/"
STEAM_STORE_SERVICE_FALLBACK_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"


@dataclass
class SteamDbSyncSummary:
    dry_run: bool
    fetched_apps: int = 0
    existing_entries: int = 0
    placeholders_found: int = 0
    names_updated: int = 0
    new_entries_added: int = 0
    skipped_invalid: int = 0
    skipped_new_limit: int = 0
    saved: bool = False
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.names_updated or self.new_entries_added)

    def add_sample(self, message: str, limit: int = 10) -> None:
        if len(self.samples) < limit:
            self.samples.append(message[:300])

    def add_error(self, message: str, limit: int = 10) -> None:
        log.error(message)
        if len(self.errors) < limit:
            self.errors.append(message[:500])

    def to_fields(self) -> dict[str, str]:
        return {
            "Mode": "DRY-RUN" if self.dry_run else "APPLY",
            "Fetched Steam apps": str(self.fetched_apps),
            "Existing DB entries": str(self.existing_entries),
            "Placeholders found": str(self.placeholders_found),
            "Names updated": str(self.names_updated),
            "New entries added": str(self.new_entries_added),
            "Skipped invalid": str(self.skipped_invalid),
            "Skipped new limit": str(self.skipped_new_limit),
            "Saved": str(self.saved),
            "Errors": str(len(self.errors)),
        }


def _clean_steam_name(name: Optional[str]) -> Optional[str]:
    clean = " ".join(str(name or "").strip().split())
    return clean or None


def _extract_app_names(payload: dict) -> dict[str, str]:
    apps = payload.get("applist", {}).get("apps") or payload.get("response", {}).get("apps") or []
    names: dict[str, str] = {}
    for item in apps:
        appid = str(item.get("appid", "")).strip()
        name = _clean_steam_name(item.get("name"))
        if appid.isdigit() and name:
            names[appid] = name
    return names


def _load_json_url(url: str, timeout: int) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "triadbot/steam-db-sync"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_store_service_app_names(
    *,
    api_key: str,
    timeout: int,
    page_size: int,
) -> dict[str, str]:
    if not api_key:
        raise RuntimeError(
            "STEAM_API_KEY is required for full Steam database sync. "
            "Add a Steam Web API key in your .env file; ManifestHub keys are not used for game names."
        )

    names: dict[str, str] = {}
    last_appid = 0
    page_size = max(1, min(int(page_size or 50000), 50000))
    last_error: Exception | None = None

    while True:
        input_json = json.dumps(
            {
                "include_games": bool(bot_config.STEAM_DB_SYNC_INCLUDE_GAMES),
                "include_dlc": bool(bot_config.STEAM_DB_SYNC_INCLUDE_DLC),
                "include_software": bool(bot_config.STEAM_DB_SYNC_INCLUDE_SOFTWARE),
                "max_results": page_size,
                "last_appid": last_appid,
            }
        )
        query = urllib.parse.urlencode({"key": api_key, "input_json": input_json})

        payload = None
        for base_url in (STEAM_STORE_SERVICE_URL, STEAM_STORE_SERVICE_FALLBACK_URL):
            try:
                payload = _load_json_url(f"{base_url}?{query}", timeout)
                break
            except Exception as exc:
                last_error = exc

        if payload is None:
            raise RuntimeError(f"Steam StoreService app list fetch failed: {last_error}")

        names.update(_extract_app_names(payload))
        response_data = payload.get("response", {})
        if not response_data.get("have_more_results"):
            break

        next_last_appid = int(response_data.get("last_appid", 0) or 0)
        if next_last_appid <= last_appid:
            raise RuntimeError("Steam StoreService pagination stopped unexpectedly")
        last_appid = next_last_appid

    if not names:
        raise RuntimeError("Steam StoreService returned no app names")
    return names


def _fetch_public_app_names(timeout: int) -> dict[str, str]:
    last_error: Exception | None = None
    for url in PUBLIC_STEAM_APP_LIST_URLS:
        try:
            names = _extract_app_names(_load_json_url(url, timeout))
            if names:
                return names
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"public Steam app list fetch failed: {last_error}")


def fetch_steam_app_names(timeout: int = bot_config.STEAM_DB_SYNC_TIMEOUT_SECONDS) -> dict[str, str]:
    api_key = bot_config.STEAM_API_KEY.strip()
    if api_key:
        return _fetch_store_service_app_names(
            api_key=api_key,
            timeout=timeout,
            page_size=bot_config.STEAM_DB_SYNC_PAGE_SIZE,
        )

    try:
        return _fetch_public_app_names(timeout)
    except Exception as exc:
        raise RuntimeError(
            "Steam app list fetch failed. Set STEAM_API_KEY in your .env file for the official "
            f"Steam StoreService sync. Last fallback error: {exc}"
        ) from exc


def sync_steam_database(
    db: DatabaseManager,
    *,
    apply: bool = False,
    include_new: bool = bot_config.STEAM_DB_SYNC_INCLUDE_NEW,
    max_new: int = bot_config.STEAM_DB_SYNC_MAX_NEW,
    max_updates: int = bot_config.STEAM_DB_SYNC_MAX_UPDATES,
    timeout: int = bot_config.STEAM_DB_SYNC_TIMEOUT_SECONDS,
) -> SteamDbSyncSummary:
    summary = SteamDbSyncSummary(dry_run=not apply)
    stats = db.get_stats()
    summary.existing_entries = stats.get("total", 0)

    try:
        steam_names = fetch_steam_app_names(timeout=timeout)
    except Exception as exc:
        summary.add_error(str(exc))
        return summary

    summary.fetched_apps = len(steam_names)

    try:
        with closing(sqlite3.connect(db.db_path)) as conn:
            cursor = conn.cursor()

            # 1. Update existing placeholders
            cursor.execute("SELECT appid, name, raw_data FROM games")
            rows = cursor.fetchall()

            for appid, name, raw_data in rows:
                if not appid.isdigit():
                    summary.skipped_invalid += 1
                    continue

                if is_placeholder_game_name(name, appid):
                    summary.placeholders_found += 1
                    steam_name = steam_names.get(appid)
                    if steam_name and steam_name != name:
                        if max_updates > 0 and summary.names_updated >= max_updates:
                            continue

                        summary.names_updated += 1
                        summary.add_sample(f"update: {name} -> {steam_name} ({appid})")
                        if apply:
                            try:
                                data = json.loads(raw_data)
                            except Exception:
                                data = {"appid": appid, "name": name, "file": False}
                            data["name"] = steam_name
                            cursor.execute(
                                "UPDATE games SET name = ?, raw_data = ? WHERE appid = ?",
                                (steam_name, json.dumps(data), appid)
                            )

            # 2. Add new entries
            if include_new:
                cursor.execute("SELECT appid FROM games")
                existing_ids = {row[0] for row in cursor.fetchall()}

                for appid in sorted(steam_names, key=lambda item: int(item) if item.isdigit() else 0):
                    if appid in existing_ids:
                        continue
                    if max_new > 0 and summary.new_entries_added >= max_new:
                        summary.skipped_new_limit += 1
                        continue

                    summary.new_entries_added += 1
                    summary.add_sample(f"new: {steam_names[appid]} ({appid})")
                    if apply:
                        raw_data = json.dumps({"appid": appid, "name": steam_names[appid], "file": False})
                        cursor.execute(
                            "INSERT INTO games (appid, name, has_file, raw_data) VALUES (?, ?, ?, ?)",
                            (appid, steam_names[appid], 0, raw_data)
                        )
                        existing_ids.add(appid)

            if apply and summary.has_changes:
                conn.commit()
                summary.saved = True
    except Exception as exc:
        summary.add_error(f"SQLite sync error: {exc}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Steam App List names to SQLite database.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--no-new", action="store_true", help="Do not add new Steam app IDs.")
    parser.add_argument("--max-new", type=int, default=bot_config.STEAM_DB_SYNC_MAX_NEW)
    parser.add_argument("--max-updates", type=int, default=bot_config.STEAM_DB_SYNC_MAX_UPDATES)
    args = parser.parse_args()

    db = DatabaseManager()
    db.load()
    summary = sync_steam_database(
        db,
        apply=args.apply,
        include_new=not args.no_new,
        max_new=args.max_new,
        max_updates=args.max_updates,
    )

    print("Steam DB sync summary")
    for name, value in summary.to_fields().items():
        print(f"{name}: {value}")
    if summary.samples:
        print("\nSamples")
        for sample in summary.samples:
            print(f"- {sample}")
    if summary.errors:
        print("\nErrors")
        for error in summary.errors:
            print(f"- {error}")
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
