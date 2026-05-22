"""
Rename database ZIP files to "Game Name (appid).zip".

Default mode is dry-run. Add --apply to perform renames.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_DIR = REPO_DIR.parent / "Database"
DEFAULT_CACHE_JSON = REPO_DIR / "data" / "appid_names_cache.json"

NUMERIC_RE = re.compile(r"^\s*[\(\[]?(?P<appid>\d{1,10})[\)\]]?\s*$")
NAMED_APPID_RE = re.compile(r"^(?P<name>.*?)\s*[\(\[](?P<appid>\d{1,10})[\)\]]\s*$")
GAME_PLACEHOLDER_RE = re.compile(r"^game\s+(?P<appid>\d{1,10})$", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"^game\s+\d+$", re.IGNORECASE)
INVALID_WINDOWS_CHARS = '<>:"/\\|?*'


@dataclass
class RenamePlan:
    source: Path
    target: Optional[Path]
    appid: Optional[str]
    status: str
    reason: str
    name_source: str = ""


def parse_appid_from_stem(stem: str) -> tuple[Optional[str], Optional[str]]:
    named = NAMED_APPID_RE.match(stem)
    if named:
        name = named.group("name").strip()
        appid = named.group("appid")
        if name:
            return appid, name

    numeric = NUMERIC_RE.match(stem)
    if numeric:
        return numeric.group("appid"), None

    placeholder = GAME_PLACEHOLDER_RE.match(stem)
    if placeholder:
        return placeholder.group("appid"), None

    return None, None


def is_placeholder_name(name: Optional[str], appid: str) -> bool:
    if not name:
        return True
    clean = " ".join(str(name).strip().split())
    return clean == appid or PLACEHOLDER_RE.match(clean) is not None


def sanitize_game_name(name: str, max_len: int = 180) -> Optional[str]:
    cleaned = "".join(" " if ch in INVALID_WINDOWS_CHARS or ord(ch) < 32 else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return None
    return cleaned[:max_len].strip(" .") or None


def set_name(
    name_map: dict[str, tuple[str, str, int]],
    appid: str,
    name: Optional[str],
    source: str,
    priority: int,
):
    if is_placeholder_name(name, appid):
        return
    clean_name = " ".join(str(name).strip().split())
    if not sanitize_game_name(clean_name):
        return
    current = name_map.get(appid)
    if not current or priority > current[2]:
        name_map[appid] = (clean_name, source, priority)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _default_sqlite_db() -> Path:
    raw = os.getenv("SQLITE_PATH", "").strip().strip('"').strip("'")
    if not raw:
        return REPO_DIR / "data" / "games.db"
    if os.name == "nt" and (raw == "/data/games.db" or raw.startswith("/data/")):
        suffix = raw.removeprefix("/data").lstrip("/\\")
        return REPO_DIR / "data" / suffix
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_DIR / path
    return path


DEFAULT_SQLITE_DB = _default_sqlite_db()


def load_sqlite_game_names(db_path: Path = DEFAULT_SQLITE_DB) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT appid, name
                FROM games
                WHERE appid IS NOT NULL
                  AND TRIM(CAST(appid AS TEXT)) != ''
                  AND name IS NOT NULL
                  AND TRIM(name) != ''
                """
            ).fetchall()
    except Exception:
        return []
    return [
        {"appid": str(appid).strip(), "name": str(name).strip()}
        for appid, name in rows
        if str(appid).strip().isdigit() and str(name).strip()
    ]


def build_name_map(database_dir: Path, db_path: Path, cache_json: Path) -> dict[str, tuple[str, str, int]]:
    name_map: dict[str, tuple[str, str, int]] = {}

    for file in database_dir.glob("*.zip"):
        appid, file_name = parse_appid_from_stem(file.stem)
        if appid and file_name:
            set_name(name_map, appid, file_name, "existing filename", 30)

    cache = load_json(cache_json, {})
    if isinstance(cache, dict):
        for appid, name in cache.items():
            set_name(name_map, str(appid), name, "steam cache", 20)

    for item in load_sqlite_game_names(db_path):
        appid = str(item.get("appid", "")).strip()
        if appid.isdigit():
            set_name(name_map, appid, item.get("name"), "SQLite games table", 10)

    return name_map


def collect_file_appids(database_dir: Path) -> set[str]:
    appids: set[str] = set()
    for file in database_dir.glob("*.zip"):
        appid, _ = parse_appid_from_stem(file.stem)
        if appid:
            appids.add(appid)
    return appids


def _extract_steam_app_list(data: dict, appids: set[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    apps = data.get("applist", {}).get("apps") or data.get("response", {}).get("apps") or []
    for item in apps:
        appid = str(item.get("appid", "")).strip()
        if appid in appids:
            name = " ".join(str(item.get("name", "")).strip().split())
            if name:
                results[appid] = name
    return results


def fetch_steam_app_list(appids: set[str], api_key: str = "", timeout: int = 60) -> dict[str, str]:
    legacy_urls = [
        "https://api.steampowered.com/ISteamApps/GetAppList/v2/?format=json",
        "https://api.steampowered.com/ISteamApps/GetAppList/v0002/?format=json",
    ]
    for url in legacy_urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            results = _extract_steam_app_list(data, appids)
            if results:
                return results
        except Exception:
            continue

    if not api_key:
        return {}

    results: dict[str, str] = {}
    last_appid = 0
    while True:
        input_json = json.dumps(
            {
                "include_games": True,
                "include_dlc": True,
                "max_results": 50000,
                "last_appid": last_appid,
            }
        )
        query = urllib.parse.urlencode({"key": api_key, "input_json": input_json})
        data = None
        for base_url in (
            "https://partner.steam-api.com/IStoreService/GetAppList/v1/",
            "https://api.steampowered.com/IStoreService/GetAppList/v1/",
        ):
            try:
                with urllib.request.urlopen(f"{base_url}?{query}", timeout=timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                continue

        if data is None:
            break

        results.update(_extract_steam_app_list(data, appids))
        response_data = data.get("response", {})
        if not response_data.get("have_more_results"):
            break
        next_last_appid = int(response_data.get("last_appid", 0) or 0)
        if next_last_appid <= last_appid:
            break
        last_appid = next_last_appid

    return results


def fetch_steam_name(appid: str, timeout: int = 10) -> Optional[str]:
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=US&l=english"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        app = data.get(appid, {})
        if not app.get("success"):
            return None
        name = app.get("data", {}).get("name")
        return " ".join(str(name).strip().split()) if name else None
    except Exception:
        return None


def make_target_path(directory: Path, appid: str, name: str) -> Optional[Path]:
    safe_name = sanitize_game_name(name)
    if not safe_name:
        return None
    return directory / f"{safe_name} ({appid}).zip"


def plan_renames(
    database_dir: Path,
    name_map: dict[str, tuple[str, str, int]],
    *,
    use_steam: bool,
    cache_json: Path,
    steam_delay: float,
    max_steam: int,
    limit: int,
) -> tuple[list[RenamePlan], dict[str, str]]:
    plans: list[RenamePlan] = []
    cache = load_json(cache_json, {})
    if not isinstance(cache, dict):
        cache = {}

    steam_fetches = 0
    files = sorted(database_dir.glob("*.zip"), key=lambda item: item.name.lower())
    if limit > 0:
        files = files[:limit]

    for file in files:
        appid, current_name = parse_appid_from_stem(file.stem)
        if not appid:
            plans.append(RenamePlan(file, None, None, "skip", "no appid found"))
            continue

        name_info = name_map.get(appid)
        if not name_info and use_steam and (max_steam <= 0 or steam_fetches < max_steam):
            fetched = fetch_steam_name(appid)
            steam_fetches += 1
            if fetched:
                cache[appid] = fetched
                set_name(name_map, appid, fetched, "steam api", 25)
                name_info = name_map.get(appid)
            if steam_delay > 0:
                time.sleep(steam_delay)

        if not name_info:
            plans.append(RenamePlan(file, None, appid, "skip", "missing valid game name"))
            continue

        target = make_target_path(database_dir, appid, name_info[0])
        if not target:
            plans.append(RenamePlan(file, None, appid, "skip", "game name is not safe as a Windows filename", name_info[1]))
            continue
        if file.name == target.name:
            plans.append(RenamePlan(file, target, appid, "ok", "already normalized", name_info[1]))
            continue

        if target.exists() and file.resolve() != target.resolve():
            plans.append(RenamePlan(file, target, appid, "skip", "target already exists", name_info[1]))
            continue

        plans.append(RenamePlan(file, target, appid, "rename", "ready", name_info[1]))

    return plans, cache


def write_report(path: Path, plans: list[RenamePlan]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["status", "appid", "source", "target", "reason", "name_source"])
        for plan in plans:
            writer.writerow([
                plan.status,
                plan.appid or "",
                str(plan.source),
                str(plan.target or ""),
                plan.reason,
                plan.name_source,
            ])


def apply_renames(plans: list[RenamePlan]) -> int:
    count = 0
    for plan in plans:
        if plan.status != "rename" or not plan.target:
            continue
        plan.source.rename(plan.target)
        count += 1
    return count


def print_summary(plans: list[RenamePlan], applied: int):
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.status] = counts.get(plan.status, 0) + 1

    print("Summary")
    for key in ["rename", "ok", "skip"]:
        print(f"  {key}: {counts.get(key, 0)}")
    print(f"  applied: {applied}")

    print("\nPreview")
    shown = 0
    for plan in plans:
        if plan.status != "rename":
            continue
        print(f"  {plan.source.name} -> {plan.target.name}")
        shown += 1
        if shown >= 20:
            remaining = counts.get("rename", 0) - shown
            if remaining > 0:
                print(f"  ... and {remaining} more")
            break


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Rename ZIP files to 'Game Name (appid).zip'.")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DATABASE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_SQLITE_DB, help="SQLite games.db path.")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_JSON)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="Actually rename files. Default is dry-run.")
    parser.add_argument("--steam-list", action="store_true", help="Fetch the full Steam app list once for missing names.")
    parser.add_argument("--steam-api-key", default=os.getenv("STEAM_API_KEY", ""), help="Steam Web API key for --steam-list.")
    parser.add_argument("--steam", action="store_true", help="Fetch missing names from Steam Store API.")
    parser.add_argument("--steam-delay", type=float, default=0.12)
    parser.add_argument("--max-steam", type=int, default=0, help="Max Steam lookups. 0 means unlimited.")
    parser.add_argument("--limit", type=int, default=0, help="Limit files scanned for testing. 0 means all.")
    args = parser.parse_args()

    database_dir = args.directory.resolve()
    if not database_dir.exists():
        raise SystemExit(f"Database directory not found: {database_dir}")

    name_map = build_name_map(database_dir, args.db.resolve(), args.cache.resolve())
    cache = load_json(args.cache.resolve(), {})
    if not isinstance(cache, dict):
        cache = {}

    if args.steam_list:
        steam_names = fetch_steam_app_list(collect_file_appids(database_dir), api_key=args.steam_api_key)
        if not steam_names:
            print(
                "Warning: Steam app list returned no names. "
                "Use --steam for per-AppID lookup or pass --steam-api-key.",
                file=sys.stderr,
            )
        for appid, name in steam_names.items():
            cache[appid] = name
            set_name(name_map, appid, name, "steam app list", 22)
        save_json(args.cache.resolve(), cache)

    plans, cache = plan_renames(
        database_dir,
        name_map,
        use_steam=args.steam,
        cache_json=args.cache.resolve(),
        steam_delay=args.steam_delay,
        max_steam=args.max_steam,
        limit=args.limit,
    )

    if args.steam:
        save_json(args.cache.resolve(), cache)

    applied = apply_renames(plans) if args.apply else 0
    if args.report:
        write_report(args.report.resolve(), plans)

    print_summary(plans, applied)
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to rename files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
