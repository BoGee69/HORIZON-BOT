from __future__ import annotations
import io
import json
import logging
import os
import random
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional
import zipfile

log = logging.getLogger(__name__)

PUBLIC_APP_LIST_URLS = [
    "https://api.steampowered.com/ISteamApps/GetAppList/v2/?format=json",
    "https://api.steampowered.com/ISteamApps/GetAppList/v0002/?format=json",
]
STORE_SERVICE_URL = "https://partner.steam-api.com/IStoreService/GetAppList/v1/"
STORE_SERVICE_FALLBACK = "https://api.steampowered.com/IStoreService/GetAppList/v1/"

INVALID_CHARS = '<>:"/\\|?*'

# Shared redist depots (VC Redist, DirectX, etc.) seen in real configs
SHARED_DEPOTS: dict[str, tuple[str, str]] = {
    "228988": ("VC 2019 Redist", "1845444d5e2cfd0ae65ae4a8fedb6e2fbf776fcc5b913ab4ac461bc9a74f8358"),
    "228989": ("VC 2022 Redist", "ad69276eb476cf06c40312df7376d63deac0c838b9a2767005be8bb306ffb853"),
    "228990": ("DirectX Jun 2010 Redist", "44d8c45ce229a11c4f231a3d2a350eaf80b0d69a8af938ec7ccca720f694b0e8"),
}

# Pool of known shared DLC/sub-apps that appear across files
SHARED_SUB_APPS = [
    "1440830", "2240110", "2927200", "3012890", "3012900",
    "3159340", "3362520", "3362530", "3362540", "3362550",
]

# Tier distribution weights
TIER_WEIGHTS = {"basic": 0.40, "medium": 0.40, "advanced": 0.20}


def _rand_hash() -> str:
    return "%064x" % random.getrandbits(256)


def _rand_manifest_id() -> str:
    return str(random.randint(10**17, 10**19 - 1))


def _rand_size() -> int:
    return random.randint(10**9, 10**11)


def sanitize_name(name: str) -> str:
    for ch in INVALID_CHARS:
        name = name.replace(ch, "-")
    name = " ".join(name.split()).strip(". ")
    return name


def _pick_tier() -> str:
    r = random.random()
    cumulative = 0.0
    for tier, weight in TIER_WEIGHTS.items():
        cumulative += weight
        if r <= cumulative:
            return tier
    return "basic"


def _generate_lua_basic(name: str, appid: str) -> str:
    return f"-- {name}\naddappid({appid}, 1)\n"


def _generate_lua_medium(name: str, appid: str) -> str:
    appid_i = int(appid)
    num_depots = random.randint(1, 3)
    lines = [f"-- {name}", f"addappid({appid}, 1)"]
    for i in range(num_depots):
        dep_id = appid_i + 1 + i
        key_hash = _rand_hash()
        lines.append(f"addappid({dep_id}, 1, \"{key_hash}\")")
    lines.append("")
    return "\n".join(lines)


def _generate_lua_advanced(name: str, appid: str) -> str:
    appid_i = int(appid)
    num_depots = random.randint(2, 6)
    add_token = random.random() < 0.25
    add_shared_redist = random.random() < 0.40
    add_shared_subs = random.random() < 0.30

    lines = [f"-- {name}"]
    if add_token:
        tok = _rand_manifest_id()
        lines.append(f"addappid({appid}, 1)")
        lines.append(f"addtoken({appid}, \"{tok}\")")
    else:
        lines.append(f"addappid({appid}, 1)")

    for i in range(num_depots):
        dep_id = appid_i + 1 + i
        key_hash = _rand_hash()
        manifest_id = _rand_manifest_id()
        size = _rand_size()
        lines.append(f"addappid({dep_id}, 1, \"{key_hash}\") -- Depot {dep_id}")
        lines.append(f"setManifestid({dep_id}, \"{manifest_id}\", {size})")

    if add_shared_redist:
        shared = random.sample(list(SHARED_DEPOTS.items()), k=random.randint(1, 3))
        for dep_id, (dep_name, dep_hash) in shared:
            manifest_id = _rand_manifest_id()
            size = _rand_size()
            lines.append(f"addappid({dep_id}, 1, \"{dep_hash}\") -- {dep_name} (Shared from App 228980)")
            lines.append(f"setManifestid({dep_id}, \"{manifest_id}\", {size})")

    if add_shared_subs:
        for sub in random.sample(SHARED_SUB_APPS, k=random.randint(1, 3)):
            lines.append(f"addappid({sub})")

    lines.append("")
    return "\n".join(lines)


def generate_lua_content(name: str, appid: str) -> str:
    tier = _pick_tier()
    if tier == "medium":
        return _generate_lua_medium(name, appid)
    elif tier == "advanced":
        return _generate_lua_advanced(name, appid)
    return _generate_lua_basic(name, appid)


def make_zip_filename(name: str, appid: str) -> str:
    safe = sanitize_name(name)
    return f"{safe} ({appid}).zip"


def create_lua_zip(name: str, appid: str) -> bytes:
    lua = generate_lua_content(name, appid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{appid}.lua", lua.encode("utf-8"))
    buf.seek(0)
    return buf.read()


def save_lua_zip(directory: Path, name: str, appid: str) -> Path | None:
    directory.mkdir(parents=True, exist_ok=True)
    zip_name = make_zip_filename(name, appid)
    dest = directory / zip_name
    if dest.exists():
        return None
    data = create_lua_zip(name, appid)
    dest.write_bytes(data)
    return dest


def fetch_all_steam_apps(
    api_key: str,
    *,
    include_games: bool = True,
    include_dlc: bool = True,
    include_software: bool = False,
    page_size: int = 50000,
    timeout: int = 120,
    appid_filter: set[str] | None = None,
) -> dict[str, str]:
    """Fetch full Steam app list. If appid_filter is set, only return matching AppIDs."""
    names: dict[str, str] = {}

    if api_key:
        last_appid = 0
        while True:
            input_json = json.dumps({
                "include_games": include_games,
                "include_dlc": include_dlc,
                "include_software": include_software,
                "max_results": page_size,
                "last_appid": last_appid,
            })
            query = f"key={api_key}&input_json={urllib.parse.quote(input_json)}"

            payload = None
            for base_url in (STORE_SERVICE_URL, STORE_SERVICE_FALLBACK):
                try:
                    url = f"{base_url}?{query}"
                    req = urllib.request.Request(url, headers={"User-Agent": "horizon/lua-sync"})
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception:
                    continue

            if not payload:
                break

            apps = payload.get("response", {}).get("apps", [])
            for item in apps:
                appid = str(item.get("appid", "")).strip()
                name = " ".join(str(item.get("name", "")).strip().split())
                if appid.isdigit() and name:
                    if appid_filter and appid not in appid_filter:
                        continue
                    names[appid] = name

            if not payload.get("response", {}).get("have_more_results"):
                break
            next_id = int(payload.get("response", {}).get("last_appid", 0) or 0)
            if next_id <= last_appid:
                break
            last_appid = next_id

        if names:
            return names

    for url in PUBLIC_APP_LIST_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "horizon/lua-sync"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            apps = data.get("applist", {}).get("apps", [])
            for item in apps:
                appid = str(item.get("appid", "")).strip()
                name = " ".join(str(item.get("name", "")).strip().split())
                if appid.isdigit() and name:
                    if appid_filter and appid not in appid_filter:
                        continue
                    names[appid] = name
            if names:
                return names
        except Exception:
            continue

    return names


def extract_appid_from_key(key: str) -> str | None:
    m = re.search(r'\((\d+)\)\.zip$', key)
    if m:
        return m.group(1)
    m = re.search(r'/(\d+)\.zip$', key)
    if m:
        return m.group(1)
    return None


def load_r2_appids_from_db(db_path: Path) -> set[str]:
    import sqlite3
    from contextlib import closing

    if not db_path.exists():
        return set()

    appids: set[str] = set()
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute("SELECT key FROM r2_inventory WHERE key LIKE 'Database/%'").fetchall()
        for row in rows:
            aid = extract_appid_from_key(row[0])
            if aid:
                appids.add(aid)
    except Exception as exc:
        log.warning("Failed to read R2 inventory from DB: %s", exc)

    return appids


def load_local_appids(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    appids: set[str] = set()
    for f in directory.glob("*.zip"):
        aid = extract_appid_from_key(f.name)
        if aid:
            appids.add(aid)
    return appids
