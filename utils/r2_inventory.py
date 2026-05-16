"""
Cached R2 inventory counts for AI chat context.
"""
from __future__ import annotations

import asyncio
import time
from threading import Lock
from typing import Any

import config as bot_config
from utils.ai_caretaker import sanitize_text
from utils.r2_presign import _BUCKET, _PRESIGN_ENABLED, _make_client

_CACHE_LOCK = Lock()
_CACHE: dict[str, Any] = {
    "prefix": None,
    "fetched_at": 0.0,
    "snapshot": None,
}


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
            snapshot["source"] = "cache"
            snapshot["cache_age_seconds"] = int(now - fetched_at)
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
                "pages_scanned": 0,
                "truncated": False,
                "source": "error",
                "error": sanitize_text(str(exc))[:300],
            }

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
