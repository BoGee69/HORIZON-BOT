"""
R2 database maintenance tools.

This module can normalize ZIP object names to "Game Name (appid).zip" and
optionally rewrite Lua files inside the ZIPs with comments removed.
"""
from __future__ import annotations

import argparse
import logging
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional

from botocore.exceptions import ClientError

import config as bot_config
from utils.lua_cleaner import clean_lua_bytes
from utils.r2_presign import _BUCKET, _PRESIGN_ENABLED, _make_client
from utils.rename_database_files import (
    DEFAULT_CACHE_JSON,
    DEFAULT_DB_JSON,
    fetch_steam_name,
    load_json,
    parse_appid_from_stem,
    sanitize_game_name,
    save_json,
    set_name,
)

log = logging.getLogger(__name__)


@dataclass
class ZipCleanResult:
    data: bytes
    changed: bool
    lua_files_checked: int = 0
    lua_files_cleaned: int = 0


@dataclass
class R2MaintenanceSummary:
    dry_run: bool
    prefix: str
    scanned: int = 0
    processed: int = 0
    rename_planned: int = 0
    rename_applied: int = 0
    lua_objects_changed: int = 0
    lua_files_checked: int = 0
    lua_files_cleaned: int = 0
    uploaded: int = 0
    copied: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.rename_planned
            or self.rename_applied
            or self.lua_objects_changed
            or self.lua_files_cleaned
            or self.uploaded
            or self.copied
            or self.deleted
        )

    def add_sample(self, message: str, limit: int = 8) -> None:
        if len(self.samples) < limit:
            self.samples.append(message[:300])

    def add_error(self, message: str, limit: int = 10) -> None:
        log.error(message)
        if len(self.errors) < limit:
            self.errors.append(message[:500])

    def to_fields(self) -> dict[str, str]:
        return {
            "Mode": "DRY-RUN" if self.dry_run else "APPLY",
            "Prefix": self.prefix or "(bucket root)",
            "Scanned": str(self.scanned),
            "Processed": str(self.processed),
            "Rename planned": str(self.rename_planned),
            "Rename applied": str(self.rename_applied),
            "Lua files cleaned": str(self.lua_files_cleaned),
            "Objects uploaded": str(self.uploaded),
            "Skipped": str(self.skipped),
            "Errors": str(len(self.errors)),
        }


def _split_key(key: str) -> tuple[str, str]:
    if "/" not in key:
        return "", key
    prefix, filename = key.rsplit("/", 1)
    return f"{prefix}/", filename


def _zip_stem_from_key(key: str) -> Optional[str]:
    _, filename = _split_key(key)
    if not filename.lower().endswith(".zip"):
        return None
    return filename[:-4]


def _target_key(source_key: str, appid: str, name: str) -> Optional[str]:
    safe_name = sanitize_game_name(name)
    if not safe_name:
        return None
    prefix, _ = _split_key(source_key)
    return f"{prefix}{safe_name} ({appid}).zip"


def _copy_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    copied = zipfile.ZipInfo(info.filename, info.date_time)
    copied.compress_type = info.compress_type
    copied.comment = info.comment
    copied.extra = info.extra
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    copied.create_system = info.create_system
    return copied


def clean_zip_lua_comments(data: bytes) -> ZipCleanResult:
    changed = False
    lua_files_checked = 0
    lua_files_cleaned = 0
    out_buffer = BytesIO()

    with zipfile.ZipFile(BytesIO(data), "r") as source_zip:
        with zipfile.ZipFile(out_buffer, "w") as target_zip:
            for item in source_zip.infolist():
                item_data = b"" if item.is_dir() else source_zip.read(item.filename)
                if not item.is_dir() and item.filename.lower().endswith(".lua"):
                    lua_files_checked += 1
                    cleaned = clean_lua_bytes(item_data)
                    if cleaned.changed:
                        item_data = cleaned.data
                        changed = True
                        lua_files_cleaned += 1

                target_zip.writestr(_copy_zip_info(item), item_data)

    if not changed:
        return ZipCleanResult(
            data=data,
            changed=False,
            lua_files_checked=lua_files_checked,
            lua_files_cleaned=0,
        )

    return ZipCleanResult(
        data=out_buffer.getvalue(),
        changed=True,
        lua_files_checked=lua_files_checked,
        lua_files_cleaned=lua_files_cleaned,
    )


def _load_games_from_json(path: Path = DEFAULT_DB_JSON) -> list[dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def _build_name_map(
    keys: Iterable[str],
    games: Optional[Iterable[dict[str, Any]]] = None,
    cache_json: Path = DEFAULT_CACHE_JSON,
) -> dict[str, tuple[str, str, int]]:
    name_map: dict[str, tuple[str, str, int]] = {}

    for key in keys:
        stem = _zip_stem_from_key(key)
        if not stem:
            continue
        appid, file_name = parse_appid_from_stem(stem)
        if appid and file_name:
            set_name(name_map, appid, file_name, "existing R2 object name", 30)

    cache = load_json(cache_json, {})
    if isinstance(cache, dict):
        for appid, name in cache.items():
            set_name(name_map, str(appid), name, "steam cache", 20)

    source_games = list(games) if games is not None else _load_games_from_json()
    for item in source_games:
        if not isinstance(item, dict):
            continue
        appid = str(item.get("id", "")).strip()
        if appid.isdigit():
            set_name(name_map, appid, item.get("name"), "games.json", 10)

    return name_map


def _list_zip_objects(client, prefix: str, max_objects: int) -> list[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    objects: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix or ""):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            if not key or key.endswith("/") or not key.lower().endswith(".zip"):
                continue
            objects.append({"Key": key, "Size": int(item.get("Size") or 0)})
            if max_objects > 0 and len(objects) >= max_objects:
                return objects

    return objects


def _object_exists(client, key: str) -> bool:
    try:
        client.head_object(Bucket=_BUCKET, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _delete_source_if_needed(client, source_key: str, target_key: str, summary: R2MaintenanceSummary) -> None:
    if source_key == target_key:
        return
    client.delete_object(Bucket=_BUCKET, Key=source_key)
    summary.deleted += 1


def _fill_missing_names_from_steam(
    objects: list[dict[str, Any]],
    name_map: dict[str, tuple[str, str, int]],
    max_lookups: int,
    delay_seconds: float,
    cache_json: Path,
) -> None:
    if max_lookups == 0:
        return

    cache = load_json(cache_json, {})
    if not isinstance(cache, dict):
        cache = {}

    fetched_count = 0
    changed = False
    for item in objects:
        if max_lookups > 0 and fetched_count >= max_lookups:
            break
        stem = _zip_stem_from_key(item["Key"])
        if not stem:
            continue
        appid, _ = parse_appid_from_stem(stem)
        if not appid or appid in name_map:
            continue

        fetched_count += 1
        name = fetch_steam_name(appid)
        if name:
            cache[appid] = name
            set_name(name_map, appid, name, "steam api", 25)
            changed = True
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    if changed:
        save_json(cache_json, cache)


def run_r2_maintenance(
    *,
    apply: bool = False,
    prefix: str = bot_config.R2_MAINTENANCE_PREFIX,
    limit: int = bot_config.R2_MAINTENANCE_MAX_OBJECTS,
    games: Optional[Iterable[dict[str, Any]]] = None,
    rename_objects: bool = bot_config.R2_MAINTENANCE_RENAME_OBJECTS,
    clean_lua_comments: bool = bot_config.R2_MAINTENANCE_CLEAN_LUA_COMMENTS,
    use_steam: bool = bot_config.R2_MAINTENANCE_STEAM_LOOKUPS,
    max_steam_lookups: int = bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS,
    max_zip_mb: int = bot_config.R2_MAINTENANCE_MAX_ZIP_MB,
    steam_delay_seconds: float = bot_config.R2_MAINTENANCE_STEAM_DELAY_SECONDS,
) -> R2MaintenanceSummary:
    summary = R2MaintenanceSummary(dry_run=not apply, prefix=prefix or "")
    if not _PRESIGN_ENABLED:
        summary.add_error("R2 credentials are incomplete; maintenance cannot run.")
        return summary

    client = _make_client()
    max_zip_bytes = max(1, max_zip_mb) * 1024 * 1024

    try:
        objects = _list_zip_objects(client, prefix or "", max(0, limit))
    except Exception as exc:
        summary.add_error(f"Failed to list R2 objects: {exc}")
        return summary

    summary.scanned = len(objects)
    object_keys = {item["Key"] for item in objects}
    name_map = _build_name_map(object_keys, games)

    if rename_objects and use_steam:
        try:
            _fill_missing_names_from_steam(
                objects,
                name_map,
                max_steam_lookups,
                steam_delay_seconds,
                DEFAULT_CACHE_JSON,
            )
        except Exception as exc:
            summary.add_error(f"Steam name lookup failed: {exc}")

    for item in objects:
        source_key = item["Key"]
        source_size = int(item.get("Size") or 0)
        target_key = source_key
        needs_rename = False
        zip_result: ZipCleanResult | None = None
        summary.processed += 1

        try:
            stem = _zip_stem_from_key(source_key)
            appid, _ = parse_appid_from_stem(stem or "") if stem else (None, None)

            if rename_objects:
                if not appid:
                    summary.skipped += 1
                    summary.add_sample(f"skip rename: {source_key} (no appid found)")
                else:
                    name_info = name_map.get(appid)
                    if not name_info:
                        summary.skipped += 1
                        summary.add_sample(f"skip rename: {source_key} (missing game name)")
                    else:
                        planned_key = _target_key(source_key, appid, name_info[0])
                        if not planned_key:
                            summary.skipped += 1
                            summary.add_sample(f"skip rename: {source_key} (unsafe game name)")
                        elif planned_key != source_key:
                            needs_rename = True
                            target_key = planned_key
                            summary.rename_planned += 1

            if needs_rename and target_key in object_keys:
                summary.skipped += 1
                summary.add_sample(f"skip object: {source_key} -> {target_key} (target exists)")
                continue

            if apply and needs_rename and _object_exists(client, target_key):
                summary.skipped += 1
                summary.add_sample(f"skip object: {source_key} -> {target_key} (target exists)")
                continue

            if clean_lua_comments:
                if source_size > max_zip_bytes:
                    summary.skipped += 1
                    summary.add_sample(f"skip lua clean: {source_key} ({source_size} bytes exceeds limit)")
                else:
                    response = client.get_object(Bucket=_BUCKET, Key=source_key)
                    body = response["Body"].read()
                    zip_result = clean_zip_lua_comments(body)
                    summary.lua_files_checked += zip_result.lua_files_checked
                    summary.lua_files_cleaned += zip_result.lua_files_cleaned
                    if zip_result.changed:
                        summary.lua_objects_changed += 1

            if summary.dry_run:
                if needs_rename:
                    summary.add_sample(f"rename: {source_key} -> {target_key}")
                if zip_result and zip_result.changed:
                    summary.add_sample(f"clean lua: {source_key} ({zip_result.lua_files_cleaned} file(s))")
                continue

            if zip_result and zip_result.changed:
                client.put_object(
                    Bucket=_BUCKET,
                    Key=target_key,
                    Body=zip_result.data,
                    ContentType="application/zip",
                )
                summary.uploaded += 1
                if needs_rename:
                    summary.rename_applied += 1
                _delete_source_if_needed(client, source_key, target_key, summary)
                continue

            if needs_rename:
                client.copy_object(
                    Bucket=_BUCKET,
                    Key=target_key,
                    CopySource={"Bucket": _BUCKET, "Key": source_key},
                    MetadataDirective="COPY",
                )
                summary.copied += 1
                summary.rename_applied += 1
                _delete_source_if_needed(client, source_key, target_key, summary)

        except zipfile.BadZipFile:
            summary.add_error(f"Bad ZIP file: {source_key}")
        except Exception as exc:
            summary.add_error(f"Failed processing {source_key}: {exc}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Maintain ZIP objects in Cloudflare R2.")
    parser.add_argument("--apply", action="store_true", help="Write changes to R2. Default is dry-run.")
    parser.add_argument("--prefix", default=bot_config.R2_MAINTENANCE_PREFIX)
    parser.add_argument("--limit", type=int, default=bot_config.R2_MAINTENANCE_MAX_OBJECTS)
    parser.add_argument("--no-rename", action="store_true")
    parser.add_argument("--no-clean-lua", action="store_true")
    parser.add_argument("--steam", action="store_true", help="Fetch missing names from Steam.")
    parser.add_argument("--max-steam", type=int, default=bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS)
    parser.add_argument("--steam-delay", type=float, default=bot_config.R2_MAINTENANCE_STEAM_DELAY_SECONDS)
    args = parser.parse_args()

    summary = run_r2_maintenance(
        apply=args.apply,
        prefix=args.prefix,
        limit=args.limit,
        rename_objects=not args.no_rename,
        clean_lua_comments=not args.no_clean_lua,
        use_steam=args.steam,
        max_steam_lookups=args.max_steam,
        steam_delay_seconds=args.steam_delay,
    )

    print("R2 maintenance summary")
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
