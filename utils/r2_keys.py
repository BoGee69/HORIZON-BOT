"""
R2 object key candidates used by download lookup and maintenance.
"""
from __future__ import annotations

from typing import Optional

from utils.rename_database_files import sanitize_game_name


R2_KEY_PATTERNS = [
    "Database/{appid}.zip",
    "Database/[{appid}].zip",
    "[{appid}].zip",
    "{appid}.zip",
]


def build_r2_key_candidates(appid: str, game_name: Optional[str] = None) -> list[str]:
    appid = str(appid).strip()
    keys: list[str] = []

    if game_name:
        safe_name = sanitize_game_name(game_name)
        if safe_name:
            keys.append(f"Database/{safe_name} ({appid}).zip")

    keys.extend(pattern.format(appid=appid) for pattern in R2_KEY_PATTERNS)

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped
