"""
Cached R2 inventory counts for AI chat context.
"""
from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock
from typing import Any

import config as bot_config
from utils.ai_caretaker import sanitize_text
from utils.r2_presign import _BUCKET, _PRESIGN_ENABLED, _make_client
from utils.rename_database_files import parse_appid_from_stem

log = logging.getLogger(__name__)

_CACHE_LOCK = Lock()
_CACHE: dict[str, Any] = {
    "prefix": None,
    "fetched_at": 0.0,
    "snapshot": None,
}


def invalidate_r2_inventory_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["prefix"] = None
        _CACHE["fetched_at"] = 0.0
        _CACHE["snapshot"] = None


def _build_snapshot_from_keys(
    *,
    keys: set[str],
    prefix: str,
    source: str,
    cache_age_seconds: int = 0,
    stale: bool = False,
) -> dict[str, Any]:
    objects_counted = len(keys)
    zip_objects_counted = 0
    named_zip_objects_counted = 0
    appid_only_zip_objects_counted = 0
    unknown_zip_objects_counted = 0

    for key in keys:
        if not key.lower().endswith(".zip"):
            continue
        zip_objects_counted += 1
        filename = key.rsplit("/", 1)[-1]
        stem = filename[:-4]
        appid, game_name = parse_appid_from_stem(stem)
        if appid and game_name:
            named_zip_objects_counted += 1
        elif appid:
            appid_only_zip_objects_counted += 1
        else:
            unknown_zip_objects_counted += 1

    return {
        "enabled": True,
        "bucket_configured": bool(_BUCKET),
        "bucket": _BUCKET,
        "prefix": prefix,
        "objects_counted": objects_counted,
        "zip_objects_counted": zip_objects_counted,
        "named_zip_objects_counted": named_zip_objects_counted,
        "appid_only_zip_objects_counted": appid_only_zip_objects_counted,
        "unknown_zip_objects_counted": unknown_zip_objects_counted,
        "pages_scanned": 0,
        "truncated": False,
        "source": source,
        "error": None,
        "cache_age_seconds": cache_age_seconds,
        "stale": stale,
    }


def _count_cached_objects_sync(prefix: str, cache_seconds: int) -> dict[str, Any] | None:
    try:
        from utils.database import R2InventoryDB

        db = R2InventoryDB()
        count = db.count(prefix)
        if count <= 0:
            return None
        keys = db.get_all_keys(prefix)
        if not keys:
            return None
        last_synced = db.last_synced_at(prefix)
        cache_age = max(0, int(time.time() - last_synced)) if last_synced else 0
        stale = bool(cache_seconds and cache_age > cache_seconds)
        return _build_snapshot_from_keys(
            keys=keys,
            prefix=prefix,
            source="sqlite-r2-inventory-cache",
            cache_age_seconds=cache_age,
            stale=stale,
        )
    except Exception as exc:
        log.warning("R2 inventory SQLite cache read failed: %s", sanitize_text(str(exc))[:300])
        return None


def _count_objects_sync(prefix: str, max_pages: int) -> dict[str, Any]:
    prefix = (prefix or "").lstrip("/")
    max_pages = max(1, int(max_pages or 1))

    if not _PRESIGN_ENABLED:
        return {
            "enabled": False,
            "bucket_configured": bool(_BUCKET),
            "bucket": _BUCKET or None,
            "prefix": prefix,
            "objects_counted": 0,
            "zip_objects_counted": 0,
            "named_zip_objects_counted": 0,
            "appid_only_zip_objects_counted": 0,
            "unknown_zip_objects_counted": 0,
            "pages_scanned": 0,
            "truncated": False,
            "source": "unavailable",
            "error": "R2 S3 credentials are not fully configured.",
        }

    client = _make_client()
    token = None
    pages_scanned = 0
    objects_counted = 0
    zip_objects_counted = 0
    named_zip_objects_counted = 0
    appid_only_zip_objects_counted = 0
    unknown_zip_objects_counted = 0
    truncated = False

    while True:
        kwargs: dict[str, Any] = {
            "Bucket": _BUCKET,
            "Prefix": prefix,
            "MaxKeys": 1000,
        }
        if token:
            kwargs["ContinuationToken"] = token

        response = client.list_objects_v2(**kwargs)
        pages_scanned += 1
        for item in response.get("Contents", []):
            key = str(item.get("Key") or "")
            if not key:
                continue
            objects_counted += 1
            if key.lower().endswith(".zip"):
                zip_objects_counted += 1
                filename = key.rsplit("/", 1)[-1]
                stem = filename[:-4]
                appid, game_name = parse_appid_from_stem(stem)
                if appid and game_name:
                    named_zip_objects_counted += 1
                elif appid:
                    appid_only_zip_objects_counted += 1
                else:
                    unknown_zip_objects_counted += 1

        token = response.get("NextContinuationToken")
        has_more = bool(response.get("IsTruncated") and token)
        if not has_more:
            break
        if pages_scanned >= max_pages:
            truncated = True
            break

    return {
        "enabled": True,
        "bucket_configured": bool(_BUCKET),
        "bucket": _BUCKET,
        "prefix": prefix,
        "objects_counted": objects_counted,
        "zip_objects_counted": zip_objects_counted,
        "named_zip_objects_counted": named_zip_objects_counted,
        "appid_only_zip_objects_counted": appid_only_zip_objects_counted,
        "unknown_zip_objects_counted": unknown_zip_objects_counted,
        "pages_scanned": pages_scanned,
        "truncated": truncated,
        "source": "live-r2-list-objects",
        "error": None,
    }


def get_r2_inventory_snapshot(
    *,
    prefix: str | None = None,
    cache_seconds: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    prefix = (prefix if prefix is not None else bot_config.R2_MAINTENANCE_PREFIX).lstrip("/")
    cache_seconds = max(0, int(cache_seconds if cache_seconds is not None else bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS))
    max_pages = max(1, int(max_pages if max_pages is not None else bot_config.AI_CHAT_R2_STATS_MAX_PAGES))
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get("snapshot")
        fetched_at = float(_CACHE.get("fetched_at") or 0)
        if cached and _CACHE.get("prefix") == prefix and now - fetched_at <= cache_seconds:
            snapshot = dict(cached)
            snapshot["source"] = "memory-cache"
            snapshot["cache_age_seconds"] = int(snapshot.get("cache_age_seconds") or 0) + int(now - fetched_at)
            return snapshot

    snapshot = _count_cached_objects_sync(prefix, cache_seconds)
    if snapshot is not None:
        with _CACHE_LOCK:
            _CACHE["prefix"] = prefix
            _CACHE["fetched_at"] = now
            _CACHE["snapshot"] = dict(snapshot)
        return snapshot

    try:
        snapshot = _count_objects_sync(prefix, max_pages)
    except Exception as exc:
        snapshot = {
            "enabled": bool(_PRESIGN_ENABLED),
            "bucket_configured": bool(_BUCKET),
            "bucket": _BUCKET or None,
            "prefix": prefix,
            "objects_counted": 0,
            "zip_objects_counted": 0,
            "named_zip_objects_counted": 0,
            "appid_only_zip_objects_counted": 0,
            "unknown_zip_objects_counted": 0,
            "pages_scanned": 0,
            "truncated": False,
            "source": "error",
            "error": sanitize_text(str(exc))[:300],
        }

    with _CACHE_LOCK:
        snapshot["cache_age_seconds"] = 0
        _CACHE["prefix"] = prefix
        _CACHE["fetched_at"] = now
        _CACHE["snapshot"] = dict(snapshot)
    return snapshot


async def get_r2_inventory_snapshot_async(
    *,
    prefix: str | None = None,
    cache_seconds: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        get_r2_inventory_snapshot,
        prefix=prefix,
        cache_seconds=cache_seconds,
        max_pages=max_pages,
    )
